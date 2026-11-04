# Dev log
## 2026-11-03 Wave 0
- Repo skeleton, baseline schema `backend/db/migrations/0001_baseline.sql`, DB security tests (31), pydantic contracts (12 tests).
- NOTE: tests use pgserver (embedded Postgres) because Docker is not installed locally; CI will use a Postgres service.
- NOTE: first harness version let `SET ROLE` roll back to superuser silently (tests passed vacuously); fixed by committing the role and asserting `current_user`. Mutation checks (remove FORCE RLS, allow terms UPDATE, remove owner trigger, remove publish gate) each make a test fail.
- DECISION: pgcrypto not used; `gen_random_uuid()` is built into Postgres 13+.
- DECISION: pre-auth functions owned by NOLOGIN `metis_auth` (BYPASSRLS, minimal privileges), because FORCE RLS applies to table owners.
- FIX (self-review): app role originally had full UPDATE/INSERT on app_user (tier escalation / forged users); now SELECT, DELETE, UPDATE(password_hash) only.
