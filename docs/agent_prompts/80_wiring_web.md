# Agent 8a: web wiring + end-to-end (Wave 3)

Worktree: `/Users/adithyarao/.superset/worktrees/metis_v0/agent8-web` (branch `wave3/web`). Python: `/Users/adithyarao/.superset/projects/metis_v0/.venv/bin/python` (Playwright 1.64 and cached Chromium are installed there; run commands from your worktree root). Do not create new virtualenvs. Do NOT commit to main, push, contact anyone, or train anything.

You own: `web/`, `tests/web/`, `tests/e2e/`, `docs/WEB_WIRING.md` (new). You may NOT edit `backend/`, `ml/`, `backend/db/migrations/`, `docs/` other than your file, or `tests/security/` (QA owns it). Anything you need from the backend that does not exist goes into your handoff as `BACKEND-GAP: <route, request, response, why>`; do not work around it by faking data.

## Goal
`web/` currently runs on FIXTURE data. Replace fixtures with the real API (`backend.main.create_app()`; read routes with `app.openapi()` in-process, since /docs is disabled) for every screen in the wireframes/spec, keeping the plain scientific design (no gradients, shadows, decorative effects; no verdict words or status colours on predictions). Fixture data may remain only for the public marketing demo on the home page.

## Build
1. `web/js/api.js`: one small client. Cookies are httponly session + CSRF cookie; send the CSRF header (`backend/accounts/config.py`: `CSRF_HEADER`, `COOKIE_CSRF`) on every mutating request; same-origin only (`credentials: "same-origin"`). Map errors to plain messages: 401 (go to sign-in), 403, 404, 409, 413, 422 (show field errors), 429 (honour `Retry-After`), 503 (busy, retry later). Never write server data into the DOM with `innerHTML`; use textContent/DOM nodes (the existing web lint tests enforce this; extend them).
2. Screens, each with loading, empty and error states:
   - Auth: register (terms/privacy/disclaimer checkbox, unchecked by default, links to `/auth/terms` documents and versions), verify (token from link), login, logout, resend verification, forgot/reset password, re-accept changed terms.
   - Project table: create/list/open/delete a project; add up to 10 compounds (SMILES, CSV, SDF, molblock paste/upload; show the 10 cap and per-row validation messages); 2D depiction and 3D conformer (labelled computed) wherever a molecule appears (spec 4.2).
   - Predict: choose endpoints and ensemble size N (only calibrated N offered; tier cap shown; time estimate shown from the API); progress from the job endpoint; every prediction cell shows value, interval or calibrated probability, N, coverage, sample size, method note (all required by the contract).
   - Uncertainty panel + model page: from `/models` and `/predictions/{id}/uncertainty`; the model page must show datasets/licences/limitations/split/benchmark/coverage and runtime at each N; if `/models` is empty show an honest "no models published yet" state, never placeholder numbers.
   - Ranking: weights, λ slider (default 0.25), hard limits, **editable target min/max per endpoint** whose defaults come from the model card `default_range`, both ranks shown, hard-limit failers show the numeric rank and the word "fail"; saving uses `PUT /projects/{id}/ranking` and reload uses `GET`. Show the stale-model-version flag the API returns.
   - Docking prep (PDB ID or upload, chains, waters, box from residues or co-crystal ligand, pocket detection, change log, Vina and GNINA downloads) and exports (PDF, SDF, CSV) with the disclaimer.
   - Account: consents (default off), `.edu` upgrade, data export/deletion request, tier and cap.
3. Structure editor: vendor **Ketcher** (EPAM, standalone build) into `web/vendor/ketcher/` for "draw structure". FIRST verify its licence from the LICENSE file shipped in the package you download and record it in `docs/LICENSES.md` via your handoff (do not edit that file). Add only the minimum CSP changes in `web/_headers`. If the download or the licence check is blocked or denied, STOP that part, finish everything else, and report `SUPERSET_WORKER_BLOCKED`-style details in your handoff; do not substitute another editor silently.
4. `tests/e2e/`: Playwright flows against a real stack started by a pytest fixture: pgserver Postgres with all migrations (see `tests/api/conftest.py` and `tests/security/wave2/w2stack.py` for how earlier tests do it), `create_app()` with a registry built from the SYNTHETIC bundle helpers in `tests/ml` (clearly named `synthetic`; never claim real predictions), an in-memory mailer that captures verification/reset links, and a static server for `web/` on the SAME origin as the API (reverse-proxy `/api` or mount both in one test ASGI app; production routing is not your decision, say what you assumed). Flows: sign up with terms -> verify -> log in -> create project -> add compounds -> predict -> view uncertainty -> rank with changed weights/limits/min-max -> export -> docking prep on the 1M17 fixture -> consents -> delete project -> delete account. Also: a second user cannot see the first user's project (404 page), 429/503 messages, keyboard-only path through sign-in and the project table, phone width (390 px) with no horizontal scroll, WebGL-missing fallback shows 2D and a notice, home-page layout and reduced motion still pass.
5. Keep `tests/web` and the existing screenshot/lint tests passing; change them only where fixtures were intentionally replaced, and list each change in your handoff. Add `docs/WEB_WIRING.md`: routes used per screen, assumptions, known gaps.

## Acceptance
`.venv/bin/python -m pytest -q tests/web tests/e2e --deselect tests/web/test_screenshots.py` is green; also run `ruff check tests/e2e` if you add Python. Report real counts. Label anything unverified.

End with the envelope:
```
SUPERSET_WORKER_DONE
task: wave3-web
summary: ...
files: ...
checks: ...
handoff: BACKEND-GAP list, Ketcher licence text and version, assumptions
```
or `SUPERSET_WORKER_BLOCKED` with `reason:` and `needs:`.
