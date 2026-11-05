# Operations (production runbook, accounts and jobs)

Scope: what the deployment must set and run for `backend/accounts`, `backend/jobs` and `backend/maintenance`.
Names only; no values belong in this file. Items marked **[unverified]** were read from code or config but not
exercised against a real production environment. Everything else is covered by a test in this repository.

## 1. Environment variables

Required in production (`METIS_ENV=production`; startup refuses on a missing, placeholder or weak value,
`backend/core/settings.py`):

| Variable | Used by | Notes |
| --- | --- | --- |
| `METIS_ENV` | all | `production` enables the required-secrets check. The image sets it by default. |
| `METIS_DATABASE_URL` | app, maintenance | Must connect as the least-privilege login role (a member of `metis_app`), never the owner or `postgres`. |
| `METIS_SECRET_KEY` | accounts | At least **64 bytes** (HS512-sized), not trivially repetitive; startup refuses a shorter one with `METIS_SECRET_KEY must be at least 64 bytes`. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(64))"`. JWT, CSRF and rate-limit pepper keys are derived from it; **rotating it signs everyone out and resets CSRF tokens**. |
| `METIS_REDIS_URL` | settings | Required by the settings check. **[unverified]** No backend code path uses Redis today; do not rely on it for sessions or rate limits (both are in Postgres). |
| `METIS_OBJECT_STORE_ENDPOINT`, `METIS_OBJECT_STORE_ACCESS_KEY`, `METIS_OBJECT_STORE_SECRET_KEY` | settings | Required by the settings check. The access key is a **bucket-scoped key you create in MinIO**, never the MinIO root credential (`MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`, which only the `minio` service receives). |

Optional tunables:

| Variable | Default | Notes |
| --- | --- | --- |
| `METIS_TERMS_VERSION` | `2026-01` | The terms/privacy/disclaimer version users must have accepted. Changing it forces re-acceptance (every authenticated domain route, reads included, answers 403 `terms_reacceptance_required`; only `/auth/*`, `GET /account`, the terms endpoints, logout and account deletion stay reachable; login and `/auth/me` report `terms_reaccept_required`). |
| `METIS_ACCOUNTS_TRUST_PROXY` | `false` | See section 2. |
| `METIS_ACCOUNTS_COOKIE_SECURE` | `true` | Leave `true`. Only set `false` for plain-HTTP local development. |
| `METIS_ACCOUNTS_SESSION_TTL_S` | `3600` | Session lifetime in seconds (minimum 60). There is no sliding refresh. |
| `METIS_ACCOUNTS_BCRYPT_COST` | `12` | 4 to 16. |
| `METIS_ACCOUNTS_VERIFY_TTL_HOURS`, `METIS_ACCOUNTS_RESET_TTL_S` | `24`, `3600` | Token lifetimes. |
| `METIS_ACCOUNTS_RL_<ROUTE>_<SCOPE>` | see `backend/accounts/config.py` | Rate limits as `(limit, window_seconds)`; includes `RL_CHANGE_PASSWORD_USER` and `RL_TERMS_ACCEPT_USER`. **The window must be 1 to 7200 seconds** (and the limit at least 1): `rate_limit_purge()` deletes buckets older than 2 hours, so a longer window would silently reset. Startup refuses a configuration outside that range. |
| `METIS_LOG_LEVEL`, `METIS_DB_POOL_SIZE`, `METIS_DB_STATEMENT_TIMEOUT_MS` | `INFO`, `5`, `30000` | |
| `METIS_OBJECT_STORE_BUCKET` | `metis` | |
| `METIS_DOCKPREP_FETCH`, `METIS_PDB2PQR`, `METIS_FPOCKET` | unset | Docking-prep feature switches and tool paths (not owned by this runbook). |

Never given to the app container: `METIS_MIGRATION_URL` (schema owner), `POSTGRES_PASSWORD`,
`METIS_APP_DB_PASSWORD`, `REDIS_PASSWORD`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` (`deploy/docker-compose.yml` allow-lists the app
environment). The allow-list contains **every** setting documented in sections 1 and 7, each as `${NAME:-code default}`, so putting
a value in `deploy/.env` is enough to change it; compose cannot pass a name that is not listed.

Health probes: `GET /health` is liveness only (touches nothing, used by the container `HEALTHCHECK`). `GET /health/ready` runs
`SELECT 1` through the app's database pool and answers `{"status":"ok"}` or `503 {"status":"unavailable"}`; it reads no tables, returns no
data or error detail and is the probe for a load balancer. The `app` service has a restart policy (`unless-stopped`) and limits
(`mem_limit` 2g, `cpus` 2, `pids_limit` 256); tune them to the host.

**Mail is not wired.** `backend/accounts/mailer.py` has only a console mailer (logs that a mail would be sent,
never the address or token) and a test double. A production deployment has no way to deliver verification or
reset mail until a real `Mailer` is injected (`app.state.mailer`). Until then new users cannot verify their
address and so cannot sign in. This is the largest operational gap in accounts.

Requirements for the real mailer (F-131). `/auth/forgot` and `/auth/resend-verification` hand the mail to
`backend.accounts.mailer.deliver()`, which runs any mailer that does not set `instant = True` on a small worker pool and
swallows (and logs by class name) its errors, so the response time and status are the same whether or not the address has
an account. This is tested with a mailer that blocks for 0.25 s and one that raises. What the real mailer must still
guarantee: `send_*` must not raise or block the interpreter indefinitely (set a socket timeout; the pool has 4
threads), and it must not be marked `instant`. `/auth/register` sends inline on purpose (both its branches send, so its
timing is uniform); a mailer outage there still answers 500 for every address alike. A residual difference of a few
milliseconds remains (an account lookup plus a token insert for registered addresses). If that ever matters, pad the
response to a fixed minimum duration in the route.

## 2. Cookies, CSRF and proxy assumptions

- Session cookie `metis_session`: `HttpOnly`, `Secure` (unless overridden), `SameSite=Lax`, path `/`, lifetime
  `METIS_ACCOUNTS_SESSION_TTL_S`. The token is an HS256 JWT with `sub`, `jti`, `iat`, `exp`, `iss` and a
  password-hash fingerprint (`pwv`).
- CSRF cookie `metis_csrf` (readable by script) plus header `X-CSRF-Token`: signed double submit, bound to the
  session `jti` (or `anon` before login), valid for the session lifetime. Every non-GET/HEAD/OPTIONS request is
  checked, including pre-login ones. Fetch `/auth/csrf` first. The web client must send the header on every
  mutation, and must use the new `csrf_token` returned by login and by `POST /auth/change-password`.
- Response headers set by the API (`ResponseHeadersMiddleware`): `Cache-Control: no-store` on every response to a request that carries the session cookie and on `/auth`, `/account`, `/consents`, `/projects`, `/jobs`, `/predictions`, `/dockprep` and `/lead` (a route that sets its own `Cache-Control` keeps it; the static web files are served by the web host, not this app), and `Strict-Transport-Security: max-age=63072000; includeSubDomains` (no `preload`) whenever the request arrived over TLS: scheme `https`, or, only with `METIS_ACCOUNTS_TRUST_PROXY=true`, a last `X-Forwarded-Proto` hop of `https`. The static site's own HSTS lives in `web/_headers`.
- Password change or reset invalidates all other sessions through the `pwv` claim. A completed reset or a password change also retires every other outstanding reset token (a second `forgot` does not retire the first, by design). Logout and password change also
  write the replaced `jti` to `session_revocation`.
- `METIS_ACCOUNTS_TRUST_PROXY=false` (default): the rate limiter uses the socket peer address. Set `true` only
  when the app is reachable exclusively through your own reverse proxy that **appends** the real client IP as the
  **last** `X-Forwarded-For` hop (only that last hop is trusted; earlier hops are client-controlled). Wrong in
  either direction is harmful: `true` with a directly exposed app lets clients choose their own rate-limit
  identity; `false` behind a proxy makes every client share the proxy address, so per-IP limits lock everyone out
  together. The proxy must also terminate TLS, because the cookies are `Secure`.
- Cookies are host-only (no `Domain`). Serving the web app and the API on different sites would break
  `SameSite=Lax` cookies; use one origin or a path-routing proxy. **[unverified]** against the real web deployment.
- Passwords: 10 to 128 characters, common-password and email-derived checks (`backend/accounts/security.py`).

## 3. Migrations

Run as the schema owner, never the app role:

    METIS_MIGRATION_URL=postgresql://<owner>@<host>/<db> python -m backend.core.migrate

The compose `migrate` service does this once before `app` starts. The runner takes an advisory lock (safe to run
concurrently), applies `backend/db/migrations/*.sql` in order, one transaction each, and refuses to continue if an
applied file was edited (checksum mismatch, exit 3) or a new file sorts before applied ones. Migration 0004
creates the `metis_janitor` NOLOGIN role and the purge functions. The login role the app uses
(`metis_app_login`, member of `metis_app`) is created by deploy tooling, not by a migration
(`docs/DB_SCHEMA.md`). **[unverified]** The exact provisioning step for that role in production is not scripted
in this repository.

## 4. Maintenance runner

`python -m backend.maintenance` calls three database functions (migration 0004) and logs counts only:

| Step | Function | Effect |
| --- | --- | --- |
| `rate_limit_buckets` | `rate_limit_purge()` | Deletes buckets older than 2 hours (floor enforced inside the function). |
| `session_revocations` | `session_purge_expired()` | Deletes revocation rows whose token has expired anyway. |
| `unverified_accounts` | `auth_purge_unverified('7 days')` | Deletes never-verified sign-ups older than 7 days (the function floors the age at 1 day); each deletion is audited. |

- One-shot: exit 0 on success, 1 if any step failed (the other steps still run), 2 if it cannot start.
- Loop mode: `python -m backend.maintenance --loop 3600` (interval at least 60 s); a SIGTERM stops it cleanly.
- Needs only `METIS_DATABASE_URL` (and optionally `METIS_ENV`, `METIS_DB_*`): it loads `DbSettings`, not the full settings, so the secret key, Redis URL and object-store variables are **not** required. In production the same URL rule applies as for the app (never the owner or `postgres`). It needs no
  extra database privilege: the app role may execute the three functions and nothing else.
- Schedule: hourly is enough. Rate-limit buckets are also purged opportunistically on 1 percent of requests, but
  the other two tables only shrink when this runner runs. Alert on a non-zero exit or on the absence of the log
  line `maintenance done: ...`.
- A compose service for it is **not** defined in `deploy/docker-compose.yml` yet (owned by Foundation); see the
  Wave 3 handoff for a suggested entry.

## 5. Jobs

Prediction jobs execute in the API process (`InProcessJobQueue`) and are recorded in the `job` table, so
`GET /jobs/{id}` survives a restart. Since migration 0006 (F-126) each queue object has a random `worker_id`, stamps it
on its rows and refreshes `heartbeat_at` every 15 s while it still runs unfinished jobs. A row left `queued`/`running`
by a dead process is failed ("interrupted by restart") the next time its owner submits a job or reads it, **only if its
heartbeat is older than 60 s** (or it has no worker, i.e. it predates the migration). A second API process or the old
container of a rolling restart therefore never fails a job the first one still runs, and a finished job's final write
only lands on a row that is still `queued`/`running` and carries that worker's id (a reaped row stays failed). After a
crash, expect up to 60 s before the dead rows stop counting against the owner's quota of 10 active jobs. The quota is
`max(rows in the database, jobs this process still tracks)`, so deleting a project (which cascades its job rows away)
cannot reset it while the jobs are still running in this process. The thread pool and the in-memory job records are
per process; run one uvicorn worker per database unless sticky routing keeps a job's polling on its process (Lead Studio
state is also per process, section 7). Job rows and logs never hold structures; failures store the exception class name only.

## 6. Account deletion

`DELETE /account/data` (password required, CSRF, rate limited) in one transaction writes a `deletion_request`
row and an audit row, deletes the user, and the database cascade removes every owned row (projects, compounds,
predictions, notes, consents, terms, sessions, jobs). The cookies are cleared.

| Step | Automated | Manual |
| --- | --- | --- |
| Live rows removed | yes (cascade, immediate) | |
| `deletion_request` recorded with `backups_purge_due = now() + 30 days` | yes | |
| Backups containing the user's data expire | | **yes**: backups must be rotated or purged within 30 days |
| `backups_purged_at` bookkeeping | | **yes**: the app role has no UPDATE on `deletion_request`; the operator calls `SELECT deletion_mark_purged('<id>')` after purging that request's backups |
| Review of due requests | | **yes**: `SELECT * FROM deletion_due_for_purge()` (id, scope, requested_at, backups_purge_due; oldest first) |

`deletion_request` has forced row-level security keyed on the owner, so a plain `SELECT` as an operator role sees no rows
(F-129). Two SECURITY DEFINER functions (migration 0006, owned by the NOLOGIN role `metis_retention`) replace it:
`deletion_due_for_purge()` is executable by `metis_readonly` and `metis_operator`, `deletion_mark_purged(id)` only by
`metis_operator` (a NOLOGIN group role: `GRANT metis_operator TO <the operator's login>` in deploy tooling). The mark
function refuses a request that is not yet due or already recorded (returns `false`) and writes an audit row with the
request id only. As a second path, `metis_readonly` may also `SELECT` the due, unpurged rows of `deletion_request`
directly (policy `deletion_due_readonly`), which returns the same rows as the function but not the others. The
`rows_purged_at` column is not used: live rows are deleted in the same transaction as the request.

The `audit_log` keeps the user id (no email) after deletion; there is no retention job for it. **[unverified]**
Backup tooling and retention in production are outside this repository.

## 7. Lead Studio (Beta), mounted at `/lead`

`leadstudio/` is mounted by `backend.main` (`/lead/api`, `/lead/library`, `/lead/profiles`). Every route needs the Metis session, CSRF on mutations and accepted terms.

| Variable | Default | Notes |
| --- | --- | --- |
| `METIS_LEAD_DOCK_URL` | unset | Allow-list entry for the DynamicBind docking service. By default only a loopback `STUDIO_DYNAMICBIND_URL` is accepted; any other host must equal this value exactly (scheme, host, port). The URL is never taken from a request. |
| `STUDIO_DYNAMICBIND_URL` | `http://127.0.0.1:8100` | Docking service. Down means `503` with a plain message; precomputed poses work without it. |
| `STUDIO_ALL_EDITS` | off | **Warning:** `1` disables the demo-safe edit filter for every user (all registry edits and far crossovers become available). For local experiments only; with `METIS_ENV=production` the app refuses to start when it is `1`. |
| `STUDIO_EMBED` | `morgan` | Embedding used for niching. `chemberta` downloads model weights from Hugging Face at run time (needs `transformers`, not shipped); leave `morgan` on a locked-down host. |
| `STUDIO_CACHE_DIR` | `/var/lib/metis/cache` (image/compose), `.cache/studio` otherwise | Adapter files (`adapters/<user id>/`) and model caches. Must be writable; the compose root filesystem is read-only, so the stack mounts a volume here. Account deletion removes the user's adapter folder. |
| `DYNAMICBIND_POLL`, `DYNAMICBIND_TIMEOUT` | 2, 900 | Seconds between polls of a docking job, and the longest a job may take. |
| `STUDIO_FORCE_MOCK` | off | Serves recorded mock runs; every response states `source: "mock"` or `"live"`. |
| `STUDIO_MAX_GENERATIONS`, `STUDIO_MAX_POPULATION`, `STUDIO_BINDING_MAX_GENERATIONS`, `STUDIO_BINDING_MAX_POPULATION` | 15, 40, set in code | Server caps. |
| `STUDIO_PER_USER_RUNS`, `STUDIO_MAX_CONCURRENT`, `STUDIO_MAX_QUEUE` | 2, 2, 4 | Per-user and per-process run limits. |
| `STUDIO_RUN_TIMEOUT` | 90 | Seconds. When it fires the run is finished with an error and its worker slot is handed back at once (a thread stuck in the engine cannot be killed; after four such abandoned threads new runs answer 503 until the process restarts). 3D embedding (3 s budget) and SMARTS matching (0.25 s per pattern, 0.5 s per candidate) have their own deadlines. |
| `STUDIO_RUNS_PER_MIN`, `STUDIO_VALIDATES_PER_MIN` | 6, 30 | Per-user limits in the shared database limiter. |
| `STUDIO_HEARTBEAT`, `STUDIO_MOCK_DELAY`, `STUDIO_REPLAY_DELAY` | 15, 0.4, 0.25 | SSE pacing. |
| `STUDIO_ORPHAN_GRACE` | 30 | Seconds. A live or mock run that no event stream reads for this long is cancelled (closed tab, dropped connection); a reconnecting `EventSource` re-attaches within the grace period and keeps the run. The per-user stream slot is released when the response ends however it ends. `0` disables the cancel. |
| `STUDIO_STRUCTURES_DIR`, `STUDIO_POSES_DIR` | bundled data | Override data directories. |

Caveats: run and live manual-session state is in memory of one process, so run a single worker (or sticky routing) for these routes. The PLAPT binding stage is not shipped (`binding_module` is absent); binding-stage runs answer 503 and the pharmacology stage works. The DQN policy needs optional torch and a checkpoint; without them the uniform policy is used.
