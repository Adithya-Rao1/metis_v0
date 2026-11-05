# Metis web

Plain HTML, CSS and ES modules. No build step. Serve this directory as the site root and the API under `/api` on the same origin
(see `docs/WEB_WIRING.md` for the routing assumption, the routes each screen uses, and known gaps).

All server calls go through `js/api.js`. Fixture data remains only for the public home-page demo (`fixtures/structures/`).

## Content Security Policy

The pages carry no inline scripts, inline event handlers or inline `style=` attributes (bar widths are set through the
CSSOM, which `style-src 'self'` allows). `_headers` holds the policy to serve, attached per path (hosts combine same-named
headers from overlapping rules, so the paths do not overlap):

    default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; img-src 'self' data: blob:;
    font-src 'self'; worker-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none';
    frame-ancestors 'none'; form-action 'self'

`connect-src 'self'` covers the API because it is served on the same origin. `/vendor/ketcher/*` (the structure editor, shown in a
same-origin iframe) gets its own policy with `style-src 'self' 'unsafe-inline'`, `connect-src 'none'` and `frame-ancestors 'self'`.
`vendor/3Dmol-min.js` contains one `eval(` call site; if the viewer is found to need it under this policy, add `'unsafe-eval'` to
`script-src` for those pages only and review. Also send HSTS at the TLS edge.

## Forms

The forms use `method="post"` with a placeholder `action`, so credentials can never end up in the URL even if the script fails to load.

## Vendored code

`vendor/VERSIONS.txt` lists 3Dmol.js, Ketcher 3.18.0 and the fonts with versions, sources, licences and sha256 hashes.

## Tests and screenshots

`tests/web` (static checks, browser tests against a mock API) and `tests/e2e` (real stack). Test screenshots are written to
`tests/web/screenshots/`, outside the served root.

## Lead Studio (Beta), `lead/`

`lead/index.html` is the Lead Optimization Studio front-end, served under `/lead/` on the same site and session. It has no sign-in of its own:
`lead/js/net.js` wraps the Metis client (`js/api.js`, which takes an optional `base`), so it shares the session cookie, the CSRF header,
the sign-in redirect (`auth.html?next=lead/index.html`) and the terms re-acceptance redirect. Its API is `/lead/api/*`, `/lead/library/*`
and `/lead/profiles/*`; the run event stream is a same-origin `EventSource`. It reuses `../vendor/3Dmol-min.js`, `../fonts/` and
`../assets/benzene.svg` (no second copies). Markup built from template strings sets computed styles with `data-style="..."`, which
`lead/js/dynstyle.js` applies through the CSSOM, so no inline style is needed under the strict CSP (`_headers` has `/lead/*` entries).
Tests: `tests/web/test_lead.py` against `LeadMockApi` in `tests/web/mockapi.py`.
