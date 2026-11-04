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
