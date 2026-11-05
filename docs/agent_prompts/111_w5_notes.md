# Agent: notes, verify-with-password, real axe (branch wave5/w5-notes)

Worktree `/Users/adithyarao/.superset/worktrees/metis_v0/w5-notes`. Python `/Users/adithyarao/.superset/projects/metis_v0/.venv/bin/python`. Do NOT push or contact anyone. You own `backend/projects/` (notes only), `backend/accounts/router.py` verify-page related bits ONLY if needed, `web/` (not `web/lead/`), `tests/web`, `tests/e2e`, `tests/api`, `docs/WEB_WIRING.md`. If a migration is needed, STOP and describe it as SCHEMA-REQUEST (the `note` table already exists; check DB_SCHEMA.md first; no migration should be needed).

## Do
1. Notes: backend create/list/update routes for notes (`/projects/{id}/compounds/{cid}/notes` or the shape the schema already implies: read `backend/projects` and the `note` table first), owner-only, 404 for others, size caps, never logged; delete exists already. Web UI on the compound page: add/edit/delete a note with confirm on delete (textContent only). Tests incl. cross-user isolation and hostile strings.
2. The verify page must send a password with `POST /auth/verify` (optional field; the F-122 design in `docs/decisions/0005-unverified-signup-credentials.md` relies on it): read the decision and the router; implement the minimal UI (a "confirm your password" field on the verify page) and tests; do not change the backend's behaviour unless the decision requires it.
3. Real axe-core: install axe-core (pinned, from the npm registry via `npm pack` into a scratch dir, or pip package `axe-playwright-python` if simpler) as a TEST-ONLY dev dependency; vendor under `tests/web/vendor/axe/` only if its licence (MPL-2.0) permits (state it) and record it in `docs/LICENSES.md` as test-only. Run it on every page incl. `lead/index.html` (light/dark), fix real serious/critical violations in web/ (and report any in web/lead/ to the integrator instead of editing there), keep the in-house audit as a second check.

## Acceptance
`pytest -q -p no:cacheprovider tests/web tests/security/web tests/e2e tests/api --deselect tests/web/test_screenshots.py` green with real counts (~12 min; use focused runs while working). No weakened assertions. Commit; do not push.

End with the envelope (task: w5-notes) or SUPERSET_WORKER_BLOCKED.
