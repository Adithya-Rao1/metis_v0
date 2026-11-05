# Agent: Wave 4 backend hardening (branch wave4/w4-backend)

Worktree: `/Users/adithyarao/.superset/worktrees/metis_v0/w4-backend`. Python `/Users/adithyarao/.superset/projects/metis_v0/.venv/bin/python`, run from your worktree. Do NOT push, contact anyone, train, or touch `web/`, `tests/security/` (QA agent owns it; you MAY remove the `xfail` marker of a finding you fix, in the existing file, and nothing else there), `tests/e2e/`. You own `backend/` (incl. a NEW migration `backend/db/migrations/0006_wave4_hardening.sql`, same style/ownership/RLS rules as 0001-0005), `leadstudio/` (only for the items below), their tests, `docs/OPERATIONS.md`, `docs/DB_SCHEMA.md`. Security is critical for this service. Details of each finding are in `/Users/adithyarao/.superset/projects/metis_v0/private/qa_wave3.md` (git-ignored; read-only; never copy exploit detail into public files or commit messages).

## Fix (each with a regression test; remove the strict-xfail marker of the matching finding in tests/security/**)
1. F-122 pre-hijack: a sign-up must not leave the first caller's password in force after the real owner verifies. Preferred: store NO usable credential until verification (password set at verification via the emailed token, or the register row's hash replaced when the owner re-registers/verifies). Pick the least invasive design, document it in a decision record `docs/decisions/0005-*.md`.
2. F-126 jobs assume one process: add a DB-side owner/lease (migration 0006, e.g. `job.worker_id` + heartbeat) or a `job_fail_interrupted()` janitor function that only fails jobs whose heartbeat is stale; a second process must not fail a live job or overwrite its row. Keep the quota semantics.
3. F-129: runbook purge query returns nothing as metis_readonly (forced RLS on deletion_request). Add a SECURITY DEFINER function `deletion_due_for_purge()` (owner a narrow role, EXECUTE only to the role that needs it) and a `deletion_mark_purged(id)`; update OPERATIONS.md to use them; test as the real roles.
4. F-123 terms gate on GETs: make every authenticated domain route, including GET, refuse (403 `terms_reaccept_required`) until accepted, except `/auth/*`, `/account` status and the terms endpoints. Apply to `/lead/*` too.
5. F-124 rate-limit terms accept and dedupe repeated accepts of the same version (no append on re-accept). F-125 invalidate outstanding reset tokens on a new forgot, a successful reset and a password change. F-127 deleting a project must not reset the active-job quota while its jobs still run (count by owner, not by project join). F-128 make the maintenance runner need only the settings it uses (fix the code or the docs, whichever is true). F-134 either cap configured rate-limit windows at the purge floor with a startup check or raise the floor; add a test. F-131: make forgot/resend response time and status independent of whether the address exists (do mail sending out of the request path or pad), as far as can be tested without a real mailer; document the remaining requirement for the real mailer.
6. Secret key: raise the minimum length to 64 bytes (HS512) with a clear startup error; update docs and `scripts/demo.py` (its generated key already qualifies).
7. Response headers (backend API side): `Cache-Control: no-store` on authenticated JSON (F-133) via middleware; keep static files cacheable. HSTS (F-132) for the API when behind TLS/`METIS_ACCOUNTS_TRUST_PROXY`; the static `_headers` file is owned by the web agent.
8. BACKEND-GAPs for the UI: `GET /auth/me` returns the email; project rename (PATCH) and delete routes for compounds, notes and measurements (owner only, 404 otherwise, audited where the schema already audits), SMILES depiction route for docking-prep ligands; `higher_is_better` on the model card (publisher + registry). Skip only what needs a spec decision, and say so.
9. Lead Studio: SSE streams and runs must be released when the client disconnects; make `/lead/api/runs` refuse when the user has not accepted terms (covered by 4); confirm no SMILES in logs (grep test). Do not change features.
10. Remove blanket `# ruff: noqa` headers in `backend/dockprep` by fixing the code, extend `ruff format --check` to all of `backend`, `ml`, `leadstudio`, `tests/foundation` if you can do it by running `ruff format` (formatting-only commit, separate), and keep `ruff check` and `mypy` clean (mypy scope unchanged).

## Acceptance
Full run `.venv/bin/python -m pytest -q -p no:cacheprovider --ignore=tests/web --ignore=tests/security/web --ignore=tests/e2e` green with real counts (it takes ~6 min; run it at the end, run focused suites while working), ruff, mypy, bandit on backend leadstudio. Do not weaken assertions; list any you changed and why. Commit logically on your branch; do not push.

End with:
```
SUPERSET_WORKER_DONE
task: w4-backend
summary: ...
files: ...
checks: ...
handoff: contract changes for the web agent (routes, shapes), anything not done and why
```
or `SUPERSET_WORKER_BLOCKED` with `reason:` and `needs:`.
