# Database schema (Wave 0 baseline)

Source of truth: `backend/db/migrations/0001_baseline.sql`. Tests: `tests/db/test_schema_security.py` (run against real Postgres via pgserver; 31 tests). Design must stay secure even though this document is public.

## Principles
1. **UUID primary keys** everywhere (audit_log uses a bigserial, never exposed).
2. **Every user-owned table has `owner_id NOT NULL` referencing `app_user`** and Row-Level Security that is ENABLED and FORCED. Policy: `owner_id = current_user_id()`, where the id comes from `SET metis.user_id` set by the API after authentication. An unset id matches nothing.
3. **RLS is the second layer.** The application also checks ownership/grants; either layer failing closed protects the data.
4. **Constraints police the data**: NOT NULL, CHECKs (lengths, enums, ranges), partial unique indexes, FKs with explicit `ON DELETE CASCADE` for user data.
5. **Least privilege roles**: `metis_app` (DML only, no DDL, no BYPASSRLS, no superuser), `metis_readonly` (sees only `model_version`, `deletion_request`: no structures, notes, credentials), `metis_auth` (NOLOGIN, owns the pre-auth functions, BYPASSRLS but only the privileges those functions need). Migrations run as a separate owner role in deploy.
6. **Pre-auth surface is five functions**, SECURITY DEFINER with pinned `search_path`: `auth_register`, `auth_lookup`, `auth_record_login`, `auth_consume_token` (and `current_user_id`). The app role cannot read `app_user` directly without a user context.

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

## Known gaps (tracked, not hidden)
- Grantee access to predictions/measurements is intentionally absent until the collaboration UI is built (BACKLOG).
- `edit`/`comment` permissions are stored but only `view` semantics are enforced at DB level; application-level enforcement is Wave 2.
- Rate-limit storage and session revocation tables are Wave 2 (Agent 4) via the integrator.
- Object-storage keys are not yet modeled.
