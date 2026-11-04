# Metis web (static fixture build)

Plain HTML, CSS and ES modules. No build step. Serve this directory as the site root.

## Content Security Policy

The pages carry no inline scripts, inline event handlers or inline `style=` attributes (bar widths are set through the
CSSOM, which `style-src 'self'` allows). `_headers` holds the policy to serve:

    default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; img-src 'self' data: blob:;
    font-src 'self'; worker-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none';
    frame-ancestors 'none'; form-action 'self'

Add the API origin to `connect-src` once the backend is wired. `vendor/3Dmol-min.js` contains one `eval(` call site; if
the viewer is found to need it under this policy, add `'unsafe-eval'` to `script-src` for those pages only and review.
Also send `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy` denying camera,
microphone and geolocation, and HSTS at the TLS edge.

## Forms

The login and sign-up forms use `method="post"` with a placeholder `action`, so credentials can never end up in the URL
even if the script fails to load. Replace the action with the real endpoint when the backend is connected.

## Screenshots

Test screenshots are written to `tests/web/screenshots/`, outside the served root.
