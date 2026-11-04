# Database schema (Wave 0 baseline)

Source of truth: `backend/db/migrations/0001_baseline.sql`. Tests: `tests/db/test_schema_security.py` (run against real Postgres via pgserver; 224 tests incl. QA red-team suite in tests/security). Design must stay secure even though this document is public.

## Principles
1. **UUID primary keys** everywhere (audit_log uses a bigserial, never exposed).
2. **Every user-owned table has `owner_id NOT NULL` referencing `app_user`** and Row-Level Security that is ENABLED and FORCED. Policy: `owner_id = current_user_id()`, where the id comes from `SET metis.user_id` set by the API after authentication. An unset id matches nothing.
3. **RLS is the second layer.** The application also checks ownership/grants; either layer failing closed protects the data.
4. **Constraints police the data**: NOT NULL, CHECKs (lengths, enums, ranges), partial unique indexes, FKs with explicit `ON DELETE CASCADE` for user data.
5. **Least privilege roles**: `metis_app` (DML only, no DDL, no BYPASSRLS, no superuser), `metis_readonly` (sees only `model_version`, `deletion_request`: no structures, notes, credentials), `metis_auth` (NOLOGIN, owns the pre-auth functions, BYPASSRLS but only the privileges those functions need). Migrations run as a separate owner role in deploy.
6. **Pre-auth surface is five SECURITY DEFINER functions with pinned `search_path`**: `auth_register` (always creates a free-tier user), `auth_lookup`, `auth_record_login`, `auth_consume_token`, `auth_issue_token` (24h TTL cap). Users cannot insert or update `auth_token`, `app_user` privileged columns, or timestamps on terms/consent. The app role cannot read `app_user` directly without a user context.

## Tables
| Table | Owner column | Notes |
| --- | --- | --- |
| app_user | id (self) | email stored lowercased, bcrypt hash only, tier free/edu/paid, lockout counters. RLS: own row. |
| auth_token | owner_id | verify/reset tokens as SHA-256 hex (64 chars) only; expiry; single use via `auth_consume_token`. |
| terms_acceptance | owner_id | Append-only (UPDATE blocked by trigger, no UPDATE/DELETE privilege). One row per document (terms, privacy, disclaimer) per acceptance with version and time. `terms_current` view gives the latest. |
| consent | owner_id | Scopes train_models, share_dataset, analytics, contact. **No rows = no consent.** One active row per scope (partial unique index). |
| project | owner_id | |
| compound | owner_id | Max 10 per project (trigger with advisory lock); owner must equal project owner (trigger). SMILES length-capped. |
| prediction | owner_id | Requires ensemble_n in (3,5,10,20), coverage in (0,1), sample size, method note, breakdown JSON; regression needs ordered interval, classification needs probability. No verdict column. |
| measurement | owner_id | User experimental data. |
| note | owner_id | Never visible to grantees. |
| grant_ | owner_id | Sharing: view/comment/edit, one active per (project, grantee), no self-grants. Grantees get read-only access to project and compound rows via RLS; notes/measurements/predictions are not shared by this baseline. |
| model_version | global | Publish gate CHECK: datasets, licenses cleared, limitations, split type, benchmark, coverage-by-N, weights hash all required. |
| deletion_request | no FK (survives user) | 30-day backup purge due date (proposed policy). |
| audit_log | actor_id | Append-only: trigger blocks UPDATE/DELETE even for superuser; app role has INSERT/SELECT only. |

## Deletion
Deleting a user cascades to every owned row. Deleting a project cascades to compounds, notes, grants and (through compounds) predictions and measurements. Object-storage files are removed by the purge job (Wave 2), recorded in `deletion_request`.

## Integrity rules added after QA review
Child rows carry `(parent_id, owner_id)` composite foreign keys, so a row can never point at another tenant's parent (checked by the FK machinery, which ignores RLS). `audit_log` and `deletion_request` have RLS (own rows only; app cannot UPDATE/DELETE deletion requests). TEMP tables are revoked. Published model versions are immutable except retiring. Identity is a session setting: the API must use `SET LOCAL`, `DISCARD ALL` on pool check-in, parameterized SQL only (see DEVLOG).

## Known gaps (tracked, not hidden)
- Grantee access to predictions/measurements is intentionally absent until the collaboration UI is built (BACKLOG).
- `edit`/`comment` permissions are stored but only `view` semantics are enforced at DB level; application-level enforcement is Wave 2.
- Rate-limit storage and session revocation tables are Wave 2 (Agent 4) via the integrator.
- Object-storage keys are not yet modeled.

## 0002 accounts support (Wave 2)
`rate_limit_bucket` (global, hashed keys only, written only through `rate_limit_hit`), `session_revocation` (RLS by owner; `session_is_revoked(jti)` callable pre-auth), `auth_upgrade_edu(user)` (verified email + `.edu` suffix checked inside SQL). Tests in tests/db.

## 0003 domain storage (Wave 2)
`project.ranking_config` (jsonb, size-bounded), `compound.conformer_molblock/conformer_method` (must be set together; always labelled computed), `compound.seq` (stable ordering), `job` table (owner-scoped RLS, composite FK to the owner's project, state/progress updatable by the app, owner and ids not), `prediction.job_id`. Tests in tests/db.

## 0004 wave 3 hardening
- **New role `metis_janitor`** (NOLOGIN, BYPASSRLS, not a member of any login role). It owns three purge functions and holds only: `SELECT(window_start), DELETE` on `rate_limit_bucket`, `SELECT(expires_at), DELETE` on `session_revocation`, `SELECT(id, email_verified_at, deleted_at, created_at), DELETE` on `app_user`, and `INSERT` on `audit_log`. No access to tenant data, emails or password hashes. `metis_auth` gained **nothing** (it still cannot delete users; `test_metis_auth_role_is_minimal`).
- `rate_limit_purge()` removes buckets older than 2 hours (floor inside the function; the app role lost its direct `DELETE` on the table). `session_purge_expired()` removes revocation rows whose token has expired. `auth_purge_unverified(interval)` deletes never-verified sign-ups older than the given age, with a 1-day floor inside the function; each deletion is audited.
- `auth_record_login` (F-60): after a lock lapses the failure count restarts at 1, so one wrong password no longer re-locks the account; an active lock still ignores both successes and failures and writes no audit row.
- `auth_reset_password(token_hash, new_hash)`: spends a single-use reset token, sets the hash, clears the lock and counters, audits, all in one statement chain. It is the only way to clear a lock, so unlocking needs possession of the mailbox. Unverified or deleted accounts are refused without saying why.
- Not solved here (tracked in BACKLOG): a squatter's password stays on an unverified row until the owner verifies; the robust fix is to set the password at verification time.
