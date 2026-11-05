# Agent: Lead Studio backend port (branch wave3b/lead-backend)

Worktree: `<worktrees>/metis_v0/lead-backend`. Python: `<project>/.venv/bin/python`, run from your worktree. Do NOT push, contact anyone, train anything, use the GPU or touch `web/` (another agent owns `web/lead/`). No new virtualenvs. SCHEMA changes: you ARE allowed to add exactly one migration, `backend/db/migrations/0005_lead_studio.sql`, following the style, ownership and RLS rules of 0001..0004 (forced RLS keyed on `metis.user_id`, owner column NOT NULL FK ON DELETE CASCADE, UUID keys, CHECK size limits, grants only to `metis_app`, nothing to metis_readonly, migration test + `tests/security` isolation test like the other tables). Security is critical for this service.

## Goal
Move the "Metis Lead Studio" from the GraphDQN repo into this repo as one feature of the same site, titled **Metis Lead Studio (Beta)**. SOURCE (read-only): `<worktrees>/GraphDQN/restyle` (branch demo/restyle-metis; GraphDQN is the owner's own MIT repo). Copy, do not symlink; never modify the source.

## Build
1. New top-level package `leadstudio/` containing the backend parts of `studio/` (server, engine, scoring, binding client, policy, embed, explain, adapt, registry, mock, schema, contracts, wiring, data incl. precomputed poses and `data/structures`, `data/targets`) plus the two packages it imports, copied in as `leadstudio/molmods/` (from `molecular_modifications`) and `leadstudio/sa/` (from `synthetic_accessibility`, with its data file). Rewrite imports. Studio-owned tests move to `tests/leadstudio/`. Do NOT copy `binding_module` (5.7 MB, lazy PLAPT import): keep the lazy import optional and make the scorer degrade as it already does when the import fails; record this in the handoff. Skip `dqn/ppo/ADMET/reward/experiments`. Drop `studio/accounts` SQLite/auth code and `studio/web`.
2. Mount it in the Metis app (`backend/main.py`, minimal) as a router/sub-app under the prefix **`/lead`**: `/lead/api/*` (was `/api/*`: health, targets, presets, runs + SSE events, molecules/validate, poses, manual/*, data), `/lead/library/*` (saved runs, manual sessions), `/lead/profiles/*` (was `/profiles`, `/library`). Do NOT mount studio `/auth`, `/me`.
3. **Authentication = Metis sign-in.** Every `/lead/*` route requires the Metis session (reuse the existing dependency used by projects routes, incl. CSRF header on mutations and terms gate). 401 for anonymous, 404 for another user's object. The studio's cookie `studio_sid` and its anonymous-session logic are removed. Run state held in memory (RunManager) must be keyed by user id so user B can never read, stream or cancel user A's run (test it).
4. **Storage:** migration 0005 creates owner-RLS tables for saved runs, manual-edit sessions (with steps) and scoring profiles, mirroring what `studio/accounts/db.py` stored (read it), with size CHECKs and per-user row caps enforced in the API. Put the DB access through the same session/identity helper the other domains use. Never log SMILES or structures. Document the tables in `docs/DB_SCHEMA.md` (short section).
5. **DynamicBind:** the studio talks to a service at `STUDIO_DYNAMICBIND_URL` (default `http://127.0.0.1:8100`). Keep that, validate the URL is loopback or explicitly allow-listed via a new setting `METIS_LEAD_DOCK_URL` (document it in docs/OPERATIONS.md). Precomputed poses must work with no service. If the service is down, return a clear 503 message, not a stack trace. No SSRF: the URL comes only from settings, never from a request.
6. **Limits (public service):** reuse the existing rate limiter on run creation, molecule validation, poses and manual preview; cap concurrent runs per user (2) and per process; cap generations/population/atoms and SMILES length server-side; runs must not block the event loop; add timeouts. The mock/real selection (`STUDIO_FORCE_MOCK`) stays; the response must always say `source: "mock"|"live"`.
7. `scripts/demo.py`: mount the lead-studio routes (it already builds the app via `create_app`; check whether they come from there) so `make demo` serves both on one address. Do not break the existing demo.
8. Add runtime deps to `requirements.txt`/`ml/requirements.txt` pinned like the others (deap, scipy, scikit-learn, torch CPU only if truly needed for the policy; if torch is only used for the optional DQN policy, make it optional and fall back, and say so). `pip-audit` clean is a CI requirement; note licenses of new deps in `docs/LICENSES.md` (deap is LGPL-3.0: flag it).
9. Tests: port the studio tests that still make sense; add tests for authz sweep (every `/lead/*` route 401 anonymously), cross-user isolation (runs, library, profiles, SSE), CSRF, rate limits, size caps, RLS isolation for the new tables. Add the new route prefixes to the authz sweep in `tests/security/`. Force mock mode in tests; no GPU or network.

## Acceptance (report real counts)
`.venv/bin/python -m pytest -q -p no:cacheprovider --ignore=tests/web --ignore=tests/security/web --ignore=tests/e2e` green (no weakened assertions; tell me any you changed), `ruff check backend ml leadstudio tests/foundation`, `mypy` (extend its scope to leadstudio only if clean; otherwise explain), bandit on leadstudio. Commit on your branch in logical commits; do not push.

End with:
```
SUPERSET_WORKER_DONE
task: lead-backend
summary: ...
files: ...
checks: ...
handoff: route contract table (method path request response), env vars, anything unverified
```
or `SUPERSET_WORKER_BLOCKED` with `reason:` and `needs:`.
