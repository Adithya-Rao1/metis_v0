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
