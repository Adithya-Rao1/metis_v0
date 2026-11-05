# Agent 9: backend completion (Wave 3)

Worktree: `<worktrees>/metis_v0/agent9-backend` (branch `wave3/backend`, from main at the commit that contains migration 0004). Python: `<project>/.venv/bin/python`, run from your worktree root. No new virtualenvs. Do NOT push, contact anyone, train anything, or touch `web/`, `ml/`, `backend/db/migrations/`, `tests/security/`, `tests/e2e/` (other agents own them). If you need a schema change, describe the exact SQL in your handoff as `SCHEMA-REQUEST:`; the integrator writes migrations.

You own: `backend/accounts/`, `backend/jobs/`, `backend/maintenance/` (new), `backend/projects/` (only where the job table needs it), their tests under `tests/accounts`, `tests/api`, `tests/maintenance` (new), `backend/main.py` only to mount or start things, and `docs/OPERATIONS.md` (new).

## Build
1. **Maintenance runner** `python -m backend.maintenance` (and importable `run_once(factory)`): calls `rate_limit_purge()`, `session_purge_expired()`, `auth_purge_unverified(interval '7 days')` (all exist in migration 0004; the app role can execute them), logs only counts (never emails or ids), exits non-zero on error. Add a loop mode with an interval, and a documented `compose` service entry in `deploy/docker-compose.yml` is NOT yours: list it in the handoff instead. Tests against pgserver.
2. **Durable job records**: make `InProcessJobQueue` (still executing in-process) write job state to the existing `job` table (migration 0003: owner RLS, composite FK to project, state/progress updatable by the app) so `GET /jobs/{id}` survives a restart, and on startup mark jobs left `queued`/`running` by a dead process as `failed` with a plain reason (`interrupted by restart`). Keep the `JobQueue` protocol, the per-user active quota and eviction behaviour. Never put structures or SMILES in job rows or logs. Tests: restart simulation (new queue instance on the same DB), cross-user isolation (user B gets 404 for A's job), quota still enforced from DB counts.
3. **Account endpoints missing for the web UI**: `POST /auth/change-password` (authenticated, CSRF, needs the current password, uses the same password policy, rate limited, invalidates other sessions via the existing pw-version claim and keeps the current one by re-issuing the cookie), `GET /auth/terms/status` + `POST /auth/terms/accept` for re-acceptance when a document version changes (use the `terms_current` view and the existing terms config; sign-in must report `terms_reaccept_required` and domain routes must refuse until accepted, with a test), `GET /account` returning tier, caps (max N), email-verified, edu flag and consents summary (check what already exists first and only add what is missing). Verify every account route against the authz sweep in `tests/security/test_wave2_redteam.py` (add new public routes to its public list only if truly public).
4. **Review pass over `backend/accounts`**: list in `docs/OPERATIONS.md` every environment variable the production deployment must set (names only, never values), the cookie/CSRF/proxy assumptions (`METIS_ACCOUNTS_TRUST_PROXY`), how to run migrations (`backend.core.migrate`), the maintenance runner schedule, and the deletion procedure (30-day backup purge is an operations step; say what is automated and what is manual). Mark anything not verified as such.

## Acceptance
`.venv/bin/python -m pytest -q tests/accounts tests/api tests/maintenance tests/foundation tests/db` green (report real counts); `ruff check backend` and `mypy` clean (CI runs both; keep the per-file ignore list from growing: fix instead of ignoring); `ruff format --check backend/core tests/foundation`. Do not weaken existing assertions; tell me which you changed and why.

End with the envelope:
```
SUPERSET_WORKER_DONE
task: wave3-backend
summary: ...
files: ...
checks: ...
handoff: SCHEMA-REQUEST list, compose/service suggestions, anything unverified
```
or `SUPERSET_WORKER_BLOCKED` with `reason:` and `needs:`.
