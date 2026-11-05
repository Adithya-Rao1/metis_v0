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
| `METIS_SECRET_KEY` | accounts | At least 32 characters, not trivially repetitive. JWT, CSRF and rate-limit pepper keys are derived from it; **rotating it signs everyone out and resets CSRF tokens**. |
| `METIS_REDIS_URL` | settings | Required by the settings check. **[unverified]** No backend code path uses Redis today; do not rely on it for sessions or rate limits (both are in Postgres). |
| `METIS_OBJECT_STORE_ENDPOINT`, `METIS_OBJECT_STORE_ACCESS_KEY`, `METIS_OBJECT_STORE_SECRET_KEY` | settings | Required by the settings check. |

Optional tunables:

| Variable | Default | Notes |
| --- | --- | --- |
| `METIS_TERMS_VERSION` | `2026-01` | The terms/privacy/disclaimer version users must have accepted. Changing it forces re-acceptance (mutating routes answer 403 `terms_reacceptance_required`; login and `/auth/me` report `terms_reaccept_required`). |
| `METIS_ACCOUNTS_TRUST_PROXY` | `false` | See section 2. |
| `METIS_ACCOUNTS_COOKIE_SECURE` | `true` | Leave `true`. Only set `false` for plain-HTTP local development. |
| `METIS_ACCOUNTS_SESSION_TTL_S` | `3600` | Session lifetime in seconds (minimum 60). There is no sliding refresh. |
| `METIS_ACCOUNTS_BCRYPT_COST` | `12` | 4 to 16. |
| `METIS_ACCOUNTS_VERIFY_TTL_HOURS`, `METIS_ACCOUNTS_RESET_TTL_S` | `24`, `3600` | Token lifetimes. |
| `METIS_ACCOUNTS_RL_<ROUTE>_<SCOPE>` | see `backend/accounts/config.py` | Rate limits as `(limit, window_seconds)`; includes `RL_CHANGE_PASSWORD_USER`. |
| `METIS_LOG_LEVEL`, `METIS_DB_POOL_SIZE`, `METIS_DB_STATEMENT_TIMEOUT_MS` | `INFO`, `5`, `30000` | |
| `METIS_OBJECT_STORE_BUCKET` | `metis` | |
| `METIS_DOCKPREP_FETCH`, `METIS_PDB2PQR`, `METIS_FPOCKET` | unset | Docking-prep feature switches and tool paths (not owned by this runbook). |

Never given to the app container: `METIS_MIGRATION_URL` (schema owner), `POSTGRES_PASSWORD`,
`METIS_APP_DB_PASSWORD`, `REDIS_PASSWORD` (`deploy/docker-compose.yml` allow-lists the app environment).

**Mail is not wired.** `backend/accounts/mailer.py` has only a console mailer (logs that a mail would be sent,
never the address or token) and a test double. A production deployment has no way to deliver verification or
reset mail until a real `Mailer` is injected (`app.state.mailer`). Until then new users cannot verify their
address and so cannot sign in. This is the largest operational gap in accounts.

## 2. Cookies, CSRF and proxy assumptions

- Session cookie `metis_session`: `HttpOnly`, `Secure` (unless overridden), `SameSite=Lax`, path `/`, lifetime
  `METIS_ACCOUNTS_SESSION_TTL_S`. The token is an HS256 JWT with `sub`, `jti`, `iat`, `exp`, `iss` and a
  password-hash fingerprint (`pwv`).
- CSRF cookie `metis_csrf` (readable by script) plus header `X-CSRF-Token`: signed double submit, bound to the
  session `jti` (or `anon` before login), valid for the session lifetime. Every non-GET/HEAD/OPTIONS request is
  checked, including pre-login ones. Fetch `/auth/csrf` first. The web client must send the header on every
  mutation, and must use the new `csrf_token` returned by login and by `POST /auth/change-password`.
- Password change or reset invalidates all other sessions through the `pwv` claim. Logout and password change also
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
- Run it with the same environment as the app (`METIS_DATABASE_URL`, `METIS_SECRET_KEY` is not needed). It needs no
  extra database privilege: the app role may execute the three functions and nothing else.
- Schedule: hourly is enough. Rate-limit buckets are also purged opportunistically on 1 percent of requests, but
  the other two tables only shrink when this runner runs. Alert on a non-zero exit or on the absence of the log
  line `maintenance done: ...`.
- A compose service for it is **not** defined in `deploy/docker-compose.yml` yet (owned by Foundation); see the
  Wave 3 handoff for a suggested entry.

## 5. Jobs

Prediction jobs execute in the API process (`InProcessJobQueue`) and are recorded in the `job` table, so
`GET /jobs/{id}` survives a restart. A job left `queued`/`running` by a dead process is marked `failed`
("interrupted by restart") the next time its owner submits a job or reads it. This assumes **one API process
owns the queue**: run a single uvicorn worker per database, or move to a separate worker process first. With
several workers each one would fail the others' live jobs. Job rows and logs never hold structures; failures store
the exception class name only.

## 6. Account deletion

`DELETE /account/data` (password required, CSRF, rate limited) in one transaction writes a `deletion_request`
row and an audit row, deletes the user, and the database cascade removes every owned row (projects, compounds,
predictions, notes, consents, terms, sessions, jobs). The cookies are cleared.

| Step | Automated | Manual |
| --- | --- | --- |
| Live rows removed | yes (cascade, immediate) | |
| `deletion_request` recorded with `backups_purge_due = now() + 30 days` | yes | |
| Backups containing the user's data expire | | **yes**: backups must be rotated or purged within 30 days |
| `rows_purged_at` / `backups_purged_at` bookkeeping | | **yes**: the app role has no UPDATE on `deletion_request`; the operator sets these after the purge |
| Review of due requests | | **yes**: `SELECT id, backups_purge_due FROM deletion_request WHERE backups_purged_at IS NULL AND backups_purge_due < now()` as the owner or `metis_readonly` |

The `audit_log` keeps the user id (no email) after deletion; there is no retention job for it. **[unverified]**
Backup tooling and retention in production are outside this repository.

## 7. Lead Studio (Beta), mounted at `/lead`

`leadstudio/` is mounted by `backend.main` (`/lead/api`, `/lead/library`, `/lead/profiles`). Every route needs the Metis session, CSRF on mutations and accepted terms.

| Variable | Default | Notes |
| --- | --- | --- |
| `METIS_LEAD_DOCK_URL` | unset | Allow-list entry for the DynamicBind docking service. By default only a loopback `STUDIO_DYNAMICBIND_URL` is accepted; any other host must equal this value exactly (scheme, host, port). The URL is never taken from a request. |
| `STUDIO_DYNAMICBIND_URL` | `http://127.0.0.1:8100` | Docking service. Down means `503` with a plain message; precomputed poses work without it. |
| `STUDIO_FORCE_MOCK` | off | Serves recorded mock runs; every response states `source: "mock"` or `"live"`. |
| `STUDIO_MAX_GENERATIONS`, `STUDIO_MAX_POPULATION`, `STUDIO_BINDING_MAX_GENERATIONS`, `STUDIO_BINDING_MAX_POPULATION` | 15, 40, set in code | Server caps. |
| `STUDIO_PER_USER_RUNS`, `STUDIO_MAX_CONCURRENT`, `STUDIO_MAX_QUEUE` | 2, 2, 4 | Per-user and per-process run limits. |
| `STUDIO_RUN_TIMEOUT` | 90 | Seconds, cooperative. |
| `STUDIO_RUNS_PER_MIN`, `STUDIO_VALIDATES_PER_MIN` | 6, 30 | Per-user limits in the shared database limiter. |
| `STUDIO_HEARTBEAT`, `STUDIO_MOCK_DELAY`, `STUDIO_REPLAY_DELAY` | 15, 0.4, 0.25 | SSE pacing. |
| `STUDIO_STRUCTURES_DIR`, `STUDIO_POSES_DIR` | bundled data | Override data directories. |

Caveats: run and live manual-session state is in memory of one process, so run a single worker (or sticky routing) for these routes. The PLAPT binding stage is not shipped (`binding_module` is absent); binding-stage runs answer 503 and the pharmacology stage works. The DQN policy needs optional torch and a checkpoint; without them the uniform policy is used.
