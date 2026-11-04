# Dev log
## 2026-11-03 Wave 0
- Repo skeleton, baseline schema `backend/db/migrations/0001_baseline.sql`, DB security tests (31), pydantic contracts (12 tests).
- NOTE: tests use pgserver (embedded Postgres) because Docker is not installed locally; CI will use a Postgres service.
- NOTE: first harness version let `SET ROLE` roll back to superuser silently (tests passed vacuously); fixed by committing the role and asserting `current_user`. Mutation checks (remove FORCE RLS, allow terms UPDATE, remove owner trigger, remove publish gate) each make a test fail.
- DECISION: pgcrypto not used; `gen_random_uuid()` is built into Postgres 13+.
- DECISION: pre-auth functions owned by NOLOGIN `metis_auth` (BYPASSRLS, minimal privileges), because FORCE RLS applies to table owners.
- FIX (self-review): app role originally had full UPDATE/INSERT on app_user (tier escalation / forged users); now SELECT, DELETE, UPDATE(password_hash) only.

## 2026-11-03 Wave 0 QA red-team review (Agent 9) and fixes
- Independent review found 7 HIGH / ~11 MEDIUM / ~13 LOW-INFO issues (details kept in git-ignored `private/`). Fixed before any push:
  forged grants and cross-tenant child rows (composite FKs on (id, owner_id)); trigger search_path/TEMP shadowing (SECURITY DEFINER cap trigger, pinned search_path, TEMP revoked); RLS on audit_log and deletion_request; users can no longer mint or reset auth tokens (auth_issue_token); auth_record_login no longer clears or extends active locks; sign-up always creates a free-tier user (edu set server-side later); server-set terms/consent timestamps and no consent deletion; NaN/Infinity and interval checks; breakdown/method-note/format checks; stricter publish gate (NULL bypass found and closed during the fix) and immutability of published models; text hygiene checks; contract validators (finite floats, value inside interval, SMILES/endpoint hygiene, ranking config checks).
- Regression tests: tests/security (strict xfail removed for every fixed finding).
- ACCEPTED / API responsibilities (still xfail, documented): F-13 identity is a session setting, so the API must use `SET LOCAL` per transaction, `DISCARD ALL` on pool check-in, parameterize all SQL, and connect with a LOGIN role that is a member of metis_app; F-14/F-15 auth functions trust the API caller; F-18 NFKC email normalization in API; F-08 lock stall (mitigated by lock_timeout 2s); F-11 predictions may reference non-published models (API enforces); F-12 default privileges; F-24/25 audit pseudonymisation and deleted-session handling; F-30 tier cap enforced by API via allowed_ensemble_sizes.

## 2026-11-03 Wave 1 (Foundation, Data and licensing, Design + QA)
- Built: backend/core (settings, redacting JSON logging, transaction-local DB identity, checksummed migration runner), deploy/ (Dockerfile, compose, env template; Docker not installed locally so unbuilt), CI workflow, license-gated data pipeline (ml/data), static front end on labeled FIXTURE data (web/), 59 browser tests.
- NOTE: the isolation=worktree option created worktrees from the wrong repo (GraphDQN) for all three agents; agents were relaunched in explicit metis_v0 worktrees (agent2-data, agent3-design; agent1 made its own).
- QA (Agent 9) found 3 HIGH, ~18 MEDIUM, many LOW (details in git-ignored private/qa_wave1.md). Fixed before this commit: prepared-statement break after DISCARD ALL, app container receiving owner credentials, CI unable to go green, log-redaction gaps, DSN/secret echoes, migration races and checksums, FreeSolv alias / junk `cleared` gate bypass, NaN labels, empty-train splits, login credentials in URL, prototype-key crash, docking overflow, heading/label issues, table clipping, missing model-page calibration content and export/report screen (fixture), screenshots moved out of web root, CSP header file.
- STILL OPEN (strict xfail, documented): F-17/F-18 (.dockerignore), F-19/F-20/F-21/F-23, MinIO root keys reach the app container, playwright unpinned in CI, Docker build unverified, draw-structure editor is a placeholder, ranking desirability d_ij and the 'fails a hard limit' display need a spec decision, remaining conformance gaps G-02, G-04..G-13 (see private/qa_wave1.md).
- Tests: 517 passed + 18 xfailed (python), 59 (browser), 48 + 3 xfailed (browser security).

## 2026-11-04 Wave 2 QA fixes
- QA found 0 critical, 6 high, 16 medium, 10 low. Fixed: F-60..F-68, F-70, F-71/71b, F-72..F-74, F-76..F-80, F-90..F-92, F-100..F-104, F-110, F-111 (strict-xfail markers removed; regression tests stay). F-18 (.dockerignore) from Wave 1 closed too.
- Open (tracked in BACKLOG): F-75 (weights unbounded at contract level), F-105 (Prediction.coverage means 1-ECE for classifiers, nominal 0.9 for regression).
- Need migration 0004 (integrator): GRANT SELECT(window_start) on rate_limit_bucket (purge fails silently without it); purge session_revocation; SQL-side lock reset in auth_record_login (F-60 now leaves a spurious login_ok audit row); unverified-account expiry and password replacement for stale unverified rows (F-61 residual); a way for reset to clear a lock.
- Not done: Docker image never built (no Docker locally); jobs still in memory (job table unwired); lint uses per-file ignores in pyproject (ruff format only on backend/core).
- Suite: 914 passed, 2 skipped, 20 xfailed before the last two marker removals; ML/red-team rerun 169 passed.

## 2026-11-04 Wave 3
- Built: backend maintenance runner, durable jobs, account endpoints, OPERATIONS.md; migration 0004 (metis_janitor role); web on the real API with vendored Ketcher 3.18.0 and per-path CSP; e2e journey tests; `make demo` launcher (synthetic models, local only).
- NOTE: demo launcher exposed a sandbox bug (503 when all slots busy); SLOT_WAIT_S = 5 s added.
- QA: 0 critical/high, 3 medium, 11 low open (see BACKLOG). Python 1153 passed, 2 skipped, 33 xfailed; browser 175 passed, 4 xfailed; ruff, format, mypy clean.
- Owner approved Wave 3. Wave 4 deliberately on hold until the demo video is filmed.
