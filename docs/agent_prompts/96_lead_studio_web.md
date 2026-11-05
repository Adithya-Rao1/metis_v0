# Agent: Lead Studio web port (branch wave3b/lead-web)

Worktree: `<worktrees>/metis_v0/lead-web`. Python: `<project>/.venv/bin/python`. Do NOT push, contact anyone or touch `backend/`, `leadstudio/`, migrations (another agent owns them; they are being written in parallel). You own `web/lead/`, the small nav/link edits in other `web/` pages, `web/_headers` (per-path CSP for the new pages), `tests/web*/` additions.

## Goal
Bring the studio front-end from `<worktrees>/GraphDQN/restyle/studio/web` (read-only source, already restyled on Metis tokens) into this repo as **Metis Lead Studio (Beta)** under `web/lead/` so one site, one sign-in, no tab switching.

## Build
1. Copy the app (index.html -> `web/lead/index.html`, js, css, fonts, assets, vendor) into `web/lead/`. Reuse Metis's shared tokens/fonts/vendored 3Dmol if identical (check hashes in `web/vendor`; do not keep two copies of the same library). Page `<title>` and the visible product name: **Metis Lead Studio (Beta)**. Keep the benzene logo.
2. API base: all studio calls go to **`/lead/api/*`** (was `/api/*`), library to **`/lead/library/*`**, profiles to **`/lead/profiles/*`**. The studio's own sign-in/account UI and its `/auth`, `/me` calls are removed: identity comes from the Metis session via `web/js/api.js` (reuse its CSRF handling, its 401 redirect to `auth.html?next=...`, its terms-reaccept handling). Replace the studio's fetch helper with it. Where the studio sent `X-Requested-With`, use the Metis CSRF header instead.
3. Navigation: add "Lead Studio (Beta)" to the Metis app nav (and home page button if it fits the wireframe: ask yourself whether it breaks the home-page bounding-box test; if so, link from the app nav only) and a "Back to Metis projects" link in the studio header. Show a visible "Beta" label and keep the note that generated molecules and poses are model outputs, not measurements.
4. Security: no inline scripts or event-handler attributes (the Metis CSP is strict; add a per-path CSP entry for `lead/index.html` in `web/_headers` only as permissive as needed: same-origin connect-src, worker-src/blob only if 3Dmol needs it); no `innerHTML` with server strings (use textContent or the escape helper the Metis pages use); no third-party requests; SSE via same-origin `EventSource` (cookies); nothing in localStorage that contains SMILES or structures.
5. The backend lives on a parallel branch. Develop against a mock API (extend `tests/web/mockapi.py`, a copy of the route contract below) so tests run without it. Contract (the backend agent implements it): GET `/lead/api/health`, `/lead/api/targets`, `/lead/api/targets/{id}`, `/lead/api/presets`, POST `/lead/api/runs` (RunConfig -> RunCreated), GET `/lead/api/runs/{id}`, SSE `/lead/api/runs/{id}/events`, POST `/lead/api/molecules/validate`, POST `/lead/api/poses`, GET `/lead/api/poses/{job}`, `/lead/api/manual/{applicable,preview,sessions...}`, `/lead/library/{runs,manual}[/id]`, `/lead/profiles[...]`. Shapes are unchanged from the studio (read `studio/web/js/api.js` and the pydantic models in the source `studio/schema.py`).
6. Tests (Playwright, same fixtures as `tests/web`): page loads, title and Beta label, redirect to sign-in when mocked 401, a run created and streamed to completion on the mock, a hostile-string test (SMILES/names with markup never become DOM), no console errors, no external requests, phone-width layout has no horizontal scroll, keyboard reachable controls. Update `tests/web` nav tests if the app nav changed. Screenshots (not committed) for your report.

## Acceptance
`.venv/bin/python -m pytest -q -p no:cacheprovider tests/web tests/security/web --deselect tests/web/test_screenshots.py` green, real counts; report any existing assertion you changed and why. Commit on your branch; do not push.

End with:
```
SUPERSET_WORKER_DONE
task: lead-web
summary: ...
files: ...
checks: ...
handoff: contract deviations you needed, anything unverified
```
or `SUPERSET_WORKER_BLOCKED` with `reason:` and `needs:`.
