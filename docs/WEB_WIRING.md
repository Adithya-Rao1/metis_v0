# Web wiring (Wave 3, Agent 8a)

`web/` is a static site (plain HTML, ES modules, no build step) that talks to the Metis API through one client, `web/js/api.js`.
Fixture data survives only in the public home-page demo (`web/fixtures/structures/`, read by `web/js/home.js`).

## Routing assumption (not a production decision)

The site and the API share ONE origin. The tests assume:

- `/api/*` goes to the FastAPI app (`backend.main.create_app()`) with the `/api` prefix stripped by the proxy;
- everything else is served from `web/`, with `web/_headers` applied (Netlify/Cloudflare-Pages semantics: all matching rules apply
  and same-named headers are combined, so the Content-Security-Policy is attached per path and the path rules do not overlap).

`tests/e2e/onesite.py` is that proxy in one ASGI app. `API_BASE = "/api"` is the only place the prefix lives (`web/js/api.js`).
Because the origin is shared: `connect-src 'self'` is enough, cookies are first-party, `credentials: "same-origin"` is used on every call,
and no CORS is needed. Deployment (Agent 1) must reproduce this routing or change `API_BASE` and `connect-src` together.

Cookies: `metis_session` (httponly, never visible to scripts) and `metis_csrf` (readable). Every mutating request sends
`X-CSRF-Token` from the `metis_csrf` cookie (constants mirrored from `backend/accounts/config.py` and asserted in `tests/web/test_static.py`).
When no CSRF cookie exists the client calls `GET /auth/csrf` first; a `403 csrf_failed` is retried once after refreshing it.

## Errors (plain messages, `describeError` in `api.js`)

| Status | Behaviour |
| --- | --- |
| 401 | on signed-in screens: redirect to `auth.html?next=...` (login itself shows "Email or password is not correct.") |
| 403 | `terms_reacceptance_required` -> re-accept screen; `email_not_verified` -> offer to resend; otherwise "no permission" |
| 404 | "Not found. It may not exist, or it may belong to another account." (the API answers 404 for other users' resources, never 403) |
| 409 | the API's message (e.g. model version changed since a prediction) |
| 413 | "That upload is too large." |
| 422 | field errors next to the input (pydantic list), per-record problems (`problems[]` on compound upload), or the API's message |
| 429 | "Too many requests. Try again in N seconds/minutes." from `Retry-After` |
| 503/504 | "The service is busy ... try again in N seconds" from `Retry-After`, or the dockprep message |

No server text is ever written with `innerHTML` (banned by `tests/web/test_static.py::test_no_dom_injection_sinks_in_own_scripts` and by the QA lint).
Every data region has loading, empty and error states (`stateBox` in `chrome.js`); errors offer "Try again".

## Routes used per screen

| Screen | Page | API |
| --- | --- | --- |
| Sign up / log in / verify / forgot / reset / resend / re-accept terms | `auth.html` | `GET /auth/terms`, `POST /auth/register`, `/auth/verify`, `/auth/login`, `/auth/resend-verification`, `/auth/forgot`, `/auth/reset`, `/terms/accept`, `GET /auth/me`, `POST /auth/logout` (emailed links carry the token as `auth.html?verify=...` and `?reset=...`; the token is removed from the URL on load) |
| Project list | `app.html` | `GET /projects`, `POST /projects`, `DELETE /projects/{id}` |
| Project table + ranking | `app.html?p=` | `GET /projects/{id}`, `GET /models`, `GET/PUT /projects/{id}/ranking`, `GET .../compounds/{cid}/depiction.svg` (as `<img>`), `GET .../conformer`, `POST /projects/{id}/measurements`, `DELETE /projects/{id}` |
| Add compounds | `add.html?p=` | `POST /projects/{id}/compounds` (`smiles`, `csv` for named SMILES and uploads, `sdf`, `molblock`); Ketcher iframe for drawing |
| Predict | `add.html?p=#predict` | `GET /models`, `POST /projects/{id}/predict`, `GET /jobs/{id}` (polled) |
| Uncertainty panel | `compound.html?p=&c=&e=` | `GET /projects/{id}`, `GET /predictions/{id}/uncertainty`, `GET /models/{endpoint}`, depiction with `?highlight=`, conformer |
| Model page | `model.html?e=` | `GET /models` (public) |
| Docking prep | `docking.html` | `POST /dockprep/proteins`, `/dockprep/sites`, `/dockprep/ligands` (and `?bundle=zip`), `GET /projects`, `GET /projects/{id}` for the ligand picker |
| Export | `report.html?p=` | `GET /projects/{id}/export?format=csv|sdf|pdf` |
| Account | `account.html` | `GET /auth/me`, `GET/POST /consents`, `GET /account/data`, `DELETE /account/data` |

## Behaviour notes

- Ranking: `PUT /projects/{id}/ranking` both saves and computes; the page loads `GET .../ranking` and, if a config was saved, re-runs the PUT to get the ranks.
  A weight's target min/max is sent only when the user changed it from the model card's `default_range` (otherwise `null`, so the model's current default applies; decision 0002).
  Both ranks are shown as `blended (property-only)`; limit failers show their numeric rank, the word "fail" and the API's limit note; `stale_model_version` / `stale_endpoints` are shown.
  The client no longer computes ranks (the old `ranking.js` and `tests/web/test_ranking.py` were removed; `tests/api` covers the engine).
- Direction (`higher_is_better`) has no default in the model card; the panel defaults to "higher" and shows the choice.
- Ensemble sizes offered = sizes up to the tier cap (`TIER_MAX_N`, mirrored from the frozen contract) that every selected endpoint has in `coverage_by_n`.
- Time estimate: before starting, the page shows `sum(runtime_by_n_seconds[N]) x compounds` from the model cards, labelled approximate; the authoritative number is `estimated_seconds` from `POST .../predict`.
- Names pasted with SMILES are sent as a CSV (`smiles,name`) because the SMILES-list request has no label field.
- Conformers are shown only with the "computed conformer, not an experimental structure" label; one shared 3Dmol viewer per page; without WebGL a notice appears and the 2D depictions stay.
- Ketcher 3.18.0 (Apache-2.0) is vendored in `web/vendor/ketcher/` (pinned, hashed in `web/vendor/VERSIONS.txt`, unmodified) and runs in a SANDBOXED iframe
  (`sandbox="allow-scripts"`, opaque origin) hosted by `web/draw/frame.html`, loaded only when the Draw tab opens. The page reads and sets the drawing through a
  postMessage bridge (`web/draw/client.js`, `web/draw/bridge.js`). `style-src 'unsafe-inline'` is granted to the frame page only; its policy has `connect-src 'none'`, `frame-ancestors 'self'` and `sandbox allow-scripts`.
- Routes assumed from the Wave 4 backend contract (mock: `tests/web/mockapi.py`): `GET /auth/me` returns `email`; `PATCH /projects/{id}` `{name}`; `DELETE /projects/{id}/compounds/{cid}`;
  `DELETE /projects/{id}/measurements/{mid}`; `POST /dockprep/depiction` `{smiles}` -> `image/svg+xml` (used for docking-prep ligands and nearest training compounds; absent route = no picture, no error);
  model card `higher_is_better` (boolean, optional: default ranking direction and a row on the model page); 403 `terms_reaccept_required` (also accepted: `terms_reacceptance_required`) on any route, GET included.
  Notes: `compound.html` lists, adds, edits and deletes the project's notes (`GET/POST /projects/{id}/notes`, `PATCH/DELETE /projects/{id}/notes/{nid}`; owner only, 404 otherwise, 5000 characters, 200 per project). Notes belong to the project (the `note` table has no compound column) and are rendered with textContent only; delete asks for confirmation.
  `auth.html?verify=` shows a "Confirm your password" field and sends `{token, password}` to `POST /auth/verify` (decision 0005, F-122); nothing is spent until the password is submitted.

## Tests

- `tests/web`: static checks plus browser tests against a MOCK API (`tests/web/mockapi.py`, Playwright interception; built from the real contract and ranking engine; test-only data).
- `tests/e2e`: the real stack in one process: pgserver Postgres with every migration, `create_app()`, an in-memory mailer, a SYNTHETIC model registry (`tests/e2e/synth_registry.py`: endpoints `synreg` and `syncls`, generated molecules and labels), and `web/` on the same origin (`tests/e2e/onesite.py`).
  The numbers in those tests prove the wiring, never prediction quality.

## Known gaps (BACKEND-GAP items are in the handoff)

- No endpoint returns a time estimate before a job starts (`POST .../predict` is the first source), so the pre-start estimate is derived from model cards.
- There is no `.edu` upgrade route (the upgrade happens when the email is verified).
- The SMILES depiction route, project rename and compound/measurement delete are wired against the Wave 4 contract above; confirm the exact paths when the backend lands (the web side is one constant per call).
- PDB-ID fetch is off unless the server sets `METIS_DOCKPREP_FETCH=1`; the page then says to upload a file.
- `tests/security/*` still reference the removed fixtures and the old page structure; QA owns them (see the handoff).
