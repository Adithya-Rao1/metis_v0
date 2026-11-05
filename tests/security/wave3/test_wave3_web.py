"""Wave 3 web red-team (Agent 10): the pages wired to the real API, the header file, vendored Ketcher.

Convention (as in Waves 1 and 2): xfail(strict=True, reason="F-xxx SEVERITY: ...") reproduces a REAL defect and becomes a hard
failure (XPASS-strict) the day it is fixed, so the marker must then be removed. Unmarked tests are protections that hold today.
Write-up and fixes: private/qa_wave3.md. The API here is tests/web/mockapi.py (test-only data); the CSP is the real web/_headers.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import urllib.request

import pytest
from hostile_api import PAYLOADS, CardApi, PoisonApi
from mockapi import PID, MockApi
from onesite import headers_for, parse_headers_file

ROOT = pathlib.Path(__file__).resolve().parents[3]
WEB = ROOT / "web"
JS = sorted((WEB / "js").glob("*.js"))
HTML = sorted(WEB.glob("*.html")) + sorted((WEB / "legal").glob("*.html"))
PROJECT = f"app.html?p={PID}"
COMPOUND = f"compound.html?p={PID}&c=cccccccc-0000-4000-8000-000000000003&e=logd"
ALL_PAGES = ["index.html", "auth.html", "app.html", PROJECT, f"add.html?p={PID}", COMPOUND, "model.html?e=logd", "docking.html", "account.html", f"report.html?p={PID}", "legal/terms.html"]
KETCHER = "/vendor/ketcher/"
DRAW_FRAME = {"/draw/frame.html", "/draw/bridge.js"}      # sandboxed host page of the vendored editor (F-121); checked in tests/web/test_draw.py + test_headers.py


def defect(fid, sev, msg):
    return pytest.mark.xfail(strict=True, reason=f"{fid} {sev}: {msg}")


# =============================================================================================== A. header file / CSP
def _site_files():
    for p in sorted(WEB.rglob("*")):
        if p.is_file() and p.name != ".DS_Store":
            yield "/" + p.relative_to(WEB).as_posix()


def _csp(path):
    h = headers_for(path, parse_headers_file(WEB / "_headers")).get("Content-Security-Policy")
    return h


def _directives(policy):
    out = {}
    for part in policy.split(";"):
        part = part.strip()
        if part:
            name, *vals = part.split()
            out[name] = vals
    return out


def test_no_csp_is_attached_by_the_catch_all_rule():
    """Same-named headers from several matching rules are COMBINED by the host (each policy then applies). A CSP on `/*` would
    intersect with the Ketcher policy and silently void its exception, and the reverse; the file keeps CSP per path."""
    rules = parse_headers_file(WEB / "_headers")
    catch_all = [h for rx, h in rules if rx.match("/anything/at/all")]
    assert catch_all and not any("Content-Security-Policy" in h for h in catch_all)


def test_every_served_file_gets_at_most_one_csp_and_every_html_page_gets_one():
    for path in _site_files():
        csp = _csp(path)
        if csp is not None:
            assert ", " not in csp and csp.count("default-src") == 1, f"{path}: two policies would be combined: {csp}"
        if path.endswith(".html"):
            assert csp, f"{path} is served without a Content-Security-Policy"


def test_only_vendor_ketcher_may_use_unsafe_inline_and_nothing_uses_unsafe_eval():
    for path in _site_files():
        csp = _csp(path)
        if not csp:
            continue
        d = _directives(csp)
        flat = " ".join(sum(d.values(), []))
        assert "'unsafe-eval'" not in flat, path
        if path.startswith(KETCHER) or path in DRAW_FRAME:
            continue
        assert "'unsafe-inline'" not in flat, f"{path} relaxes the policy"
        assert d["script-src"] == ["'self'", "'wasm-unsafe-eval'"], path
        assert d["style-src"] == ["'self'"], path
        assert d["connect-src"] == ["'self'"] and d["object-src"] == ["'none'"] and d["base-uri"] == ["'none'"], path
        assert d["frame-ancestors"] == ["'none'"] and d["form-action"] == ["'self'"], path
        assert not any(re.match(r"https?:|\*", v) for vals in d.values() for v in vals), path


def test_ketcher_exception_is_exactly_style_inline_and_nothing_else():
    ref = _directives(_csp("/add.html"))
    kets = {p for p in _site_files() if p.startswith(KETCHER)}
    assert kets
    for path in kets:
        d = _directives(_csp(path))
        assert d["style-src"] == ["'self'", "'unsafe-inline'"], path
        assert d["script-src"] == ["'self'", "'wasm-unsafe-eval'"], path           # scripts stay locked: no inline, no eval
        assert d["connect-src"] == ["'none'"], path                                  # cannot call the API or anything else
        assert d["frame-ancestors"] == ["'self'"] and d["form-action"] == ["'none'"] and d["object-src"] == ["'none'"] and d["base-uri"] == ["'none'"], path
        assert d["default-src"] == ["'self'"] and set(d) == set(ref), path
        assert d["img-src"] == ref["img-src"] and d["worker-src"] == ref["worker-src"], path
        assert d["font-src"] == ["'self'", "data:"]


def test_hsts_is_set_for_every_path():
    rules = parse_headers_file(WEB / "_headers")
    h = headers_for("/index.html", rules)
    assert "max-age=" in h.get("Strict-Transport-Security", "")


def test_files_without_csp_are_inert_text_only():
    bare = [p for p in _site_files() if _csp(p) is None and p != "/_headers"]     # _headers is a host directive file, not served by Netlify/Pages
    assert all(p.endswith((".txt", ".md")) for p in bare), bare


def test_served_headers_match_the_file(site):
    for path, want_inline in (("/add.html", False), ("/account.html", False), ("/vendor/ketcher/index.html", True), ("/js/api.js", False), ("/legal/terms.html", False)):
        with urllib.request.urlopen(site.url + path) as r:
            csp = r.headers["Content-Security-Policy"]
            assert ("'unsafe-inline'" in csp) is want_inline, path
            assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["Referrer-Policy"] == "no-referrer", path
            assert "camera=()" in r.headers["Permissions-Policy"] and r.headers["Cross-Origin-Opener-Policy"] == "same-origin", path


# ============================================================================================ B. vendored Ketcher
def _versions():
    txt = (WEB / "vendor/VERSIONS.txt").read_text()
    return {m.group(1): m.group(2) for m in re.finditer(r"^\s+((?:static/|index\.html|LICENSE|NOTICE)[\w./-]*): ([0-9a-f]{64})\s*$", txt, re.M)}


def test_vendored_ketcher_files_match_recorded_hashes_and_nothing_executable_is_unrecorded():
    rec = _versions()
    assert len(rec) >= 8
    base = WEB / "vendor/ketcher"
    for rel, digest in rec.items():
        assert hashlib.sha256((base / rel).read_bytes()).hexdigest() == digest, rel
    # any script, page, stylesheet, wasm or worker that is shipped must be pinned: an extra file would run unreviewed
    extra = [p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file() and p.suffix in {".js", ".html", ".css", ".wasm", ".mjs", ".svg"} and p.relative_to(base).as_posix() not in rec]
    assert not extra, extra
    txt = (WEB / "vendor/VERSIONS.txt").read_text()
    assert "484e7f10a0e74808ae5f43f0e6448db6f4d884bdd2189aa271108474167a3e7e" in txt     # zip digest recorded


def test_ketcher_index_has_no_inline_script_and_no_remote_reference():
    t = (WEB / "vendor/ketcher/index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", t) and not re.search(r"\son[a-z]+\s*=", t)
    assert not re.search(r"(?:src|href)=[\"']?(?:https?:)?//", t)


def test_ketcher_runs_under_its_csp_with_no_network_and_no_storage(hpage):
    pg = hpage()
    pg.goto(f"{pg.base}/add.html?p={PID}")
    pg.wait_for_selector("#add-frame:not([hidden])")
    pg.click("#t-draw")
    pg.wait_for_function("() => document.getElementById('ketcher').contentWindow !== null")
    fr = next(f for f in pg.frames if "draw/frame.html" in f.url)
    fr.wait_for_function("() => !!window.ketcher", timeout=90000)       # the sandboxed frame; the page itself cannot see into it
    smi = fr.evaluate("window.ketcher.getSmiles()")
    assert isinstance(smi, str)
    ext = [r for r in pg.requests if not r.startswith(pg.base) and not r.startswith(("data:", "blob:"))]
    assert not ext, ext
    assert [e for e in pg.errors if re.search(r"Content Security Policy|Refused to", e)] == []
    assert pg.evaluate("Object.keys(localStorage).concat(Object.keys(sessionStorage))") == []     # nothing from the editor reaches the app origin's storage (F-121)
    assert pg.evaluate("document.cookie.split(';').map(c => c.trim().split('=')[0]).filter(Boolean)") == ["metis_csrf"]


def test_ketcher_is_isolated_from_the_app_origin():
    html = (WEB / "add.html").read_text()
    tag = re.search(r"<iframe[^>]*id=\"ketcher\"[^>]*>", html).group(0)
    sandboxed = re.search(r"\bsandbox=\"([^\"]*)\"", tag)
    other_origin = re.search(r"data-src=\"https?://", tag)
    assert other_origin or (sandboxed and "allow-same-origin" not in sandboxed.group(1))


# ================================================================================================ C. XSS / sinks
BANNED = re.compile(
    r"\.innerHTML|\.outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new\s+Function|(?<![\w.])Function\s*\(|\bimport\s*\(|postMessage|window\.open|\.srcdoc|"
    r"createContextualFragment|setTimeout\(\s*[\"'`]|setInterval\(\s*[\"'`]|document\.domain|\.insertAdjacent|DOMParser|\.execCommand|"
    r"setAttribute\(\s*[\"']on|location\s*=[^=]|location\.(?:href|assign|replace)\s*=[^=]|addEventListener\(\s*[\"']message"
)


def test_no_script_sinks_in_own_js_beyond_navigation_with_validated_targets():
    for f in JS:
        t = re.sub(r"//[^\n]*", "", f.read_text())
        hits = BANNED.findall(t)
        # location.assign/replace(...) calls (method form) are allowed only with literals, template strings of validated ids, or safeNext
        assert not hits, (f.name, hits)
        for m in re.finditer(r"location\.(?:assign|replace)\(([^)]*)\)", t):
            arg = m.group(1)
            assert re.fullmatch(r"\s*(\"[a-z_.]+\"|`app\.html\?p=\$\{[a-z]+\.id\}`|`app\.html\?p=\$\{p\.id\}`|safeNext\([^)]*\)|`auth\.html\?\$\{q\}`|\"index\.html\"|\"auth\.html\"|\"app\.html\")\s*", arg) or "safeNext" in arg, (f.name, arg)


def test_browser_storage_is_never_used():
    src = "".join(f.read_text() for f in JS)
    assert not re.search(r"localStorage|sessionStorage|indexedDB|caches\.|navigator\.storage|document\.cookie\s*=", src)
    # the only cookie read is the CSRF cookie
    assert set(re.findall(r"readCookie\(([A-Za-z_\"']+)\)", src)) - {"name"} <= {"COOKIE_CSRF"}          # `name` is the definition itself


def test_storage_and_cookies_stay_empty_after_using_the_app(hpage):
    pg = hpage()
    for u in (f"account.html", PROJECT, f"add.html?p={PID}", COMPOUND, "model.html?e=logd", "docking.html"):
        pg.goto(f"{pg.base}/{u}")
        pg.wait_for_timeout(500)
    pg.goto(f"{pg.base}/account.html")
    pg.wait_for_selector("[data-consent]")
    pg.check("#consent-analytics")
    pg.wait_for_timeout(300)
    assert pg.evaluate("Object.keys(localStorage).length + Object.keys(sessionStorage).length") == 0
    assert pg.evaluate("indexedDB.databases().then(d => d.length)") == 0
    assert pg.evaluate("document.cookie.split(';').map(c => c.trim().split('=')[0]).filter(Boolean)") == ["metis_csrf"]


@pytest.mark.parametrize("status,detail", [
    (409, PAYLOADS[0]), (413, {"message": PAYLOADS[1]}), (422, {"message": PAYLOADS[0]}), (422, [{"loc": ["body", PAYLOADS[1]], "msg": PAYLOADS[0], "type": "x"}]),
    (503, {"message": PAYLOADS[2]}), (500, PAYLOADS[3]), (400, {"code": PAYLOADS[0], "message": PAYLOADS[0]}), (403, PAYLOADS[1]), (404, PAYLOADS[0]),
])
@pytest.mark.parametrize("url", ["app.html", PROJECT, f"add.html?p={PID}", COMPOUND, "model.html?e=logd", "account.html", "docking.html", f"report.html?p={PID}"])
def test_server_error_text_is_inert_on_every_screen(hpage, url, status, detail):
    api = MockApi()
    for rx in (r"^/projects$", rf"^/projects/{PID}$", r"^/models", r"/uncertainty$", r"^/consents$", r"/export$", r"^/auth/me$", r"/ranking$"):
        if url.startswith("account") and rx == r"^/auth/me$":
            continue
        api.overrides[("GET", rx)] = (status, {"detail": detail}, {"retry-after": "7"})
    api.overrides[("PUT", r"/ranking$")] = (status, {"detail": detail}, {})
    pg = hpage(api=api)
    pg.goto(f"{pg.base}/{url}")
    pg.wait_for_timeout(700)
    assert pg.evaluate("window.__xss") is None
    assert pg.evaluate("document.querySelectorAll('img[src=\"x\"], [onerror], [onload], body script:not([src]), svg[onload]').length") == 0


def test_hostile_field_errors_and_per_record_problems_are_text(hpage):
    api = MockApi()
    api.overrides[("POST", "/auth/register")] = (422, {"detail": [{"loc": ["body", 'email"] , x[name="'], "msg": PAYLOADS[0], "type": "x"}, {"loc": ["body", "password"], "msg": PAYLOADS[1], "type": "x"}]}, {})
    api.overrides[("POST", rf"/projects/{PID}/compounds$")] = (422, {"detail": {"message": PAYLOADS[0], "problems": [{"record": PAYLOADS[1], "error": PAYLOADS[0]}]}}, {})
    api.signed_in = False
    pg = hpage(api=api)
    pg.goto(f"{pg.base}/auth.html")
    pg.wait_for_function("() => document.getElementById('terms-version').textContent.includes('version')")
    pg.fill("#su-email", "a@example.org")
    pg.fill("#su-pass", "correct horse battery")
    pg.check("#accept")
    pg.click("#signup-submit")
    pg.wait_for_function("() => document.getElementById('signup-msg').textContent !== 'Working...' && document.getElementById('signup-msg').textContent.length > 0")
    assert pg.evaluate("window.__xss") is None and "<img" in pg.inner_text("#signup-msg")
    api.signed_in = True
    pg.goto(f"{pg.base}/add.html?p={PID}")
    pg.fill("#smi", "CCO")
    pg.click("#add-submit")
    pg.wait_for_selector("#add-problems .tag")
    assert pg.evaluate("window.__xss") is None and PAYLOADS[1] in pg.inner_text("#add-problems")
    assert pg.evaluate("document.querySelectorAll('#add-problems img, #add-problems svg, #add-msg img').length") == 0


def test_dataset_links_only_http_and_https_with_noopener(hpage, synth_registry):
    from hostile_api import card_json

    reg, _ = synth_registry
    card = card_json(reg, "synreg")
    urls = ["javascript:window.__xss=1", "data:text/html,<script>window.__xss=1</script>", "//evil.example/x", "https://example.org/ok", "http://example.org/plain", "vbscript:x", " javascript:window.__xss=1"]
    card["datasets"] = [{**card["datasets"][0], "name": f"d{i}", "url": u} for i, u in enumerate(urls)]
    pg = hpage(api=CardApi([card]))
    pg.goto(f"{pg.base}/model.html?e=synreg")
    pg.wait_for_selector("#datasets-table")
    hrefs = pg.eval_on_selector_all("#datasets-table a", "els => els.map(a => [a.getAttribute('href'), a.rel, a.target])")
    assert {h for h, _, _ in hrefs} <= {"https://example.org/ok", "http://example.org/plain"}
    assert all("noopener" in rel and t == "_blank" for _, rel, t in hrefs)
    assert pg.evaluate("window.__xss") is None


# ====================================================================================== D. redirects / tokens in URLs
NEXT_CASES = ["//evil.example/x", "https://evil.example", "http://evil.example/app.html", "javascript:alert(1)", "\\\\evil.example", "/app.html", "../app.html",
              "app.html\n", "app.html?x=<script>", "APP.html", "app.html#//evil", "app.html?p=%0d%0a", "data:text/html,x", "app.html@evil.example", "evil.example/app.html", "", "app.html?p=1&q=2", "report.html?p=3"]


def test_safe_next_only_returns_same_site_page_names(hpage):
    pg = hpage()
    pg.goto(f"{pg.base}/auth.html")
    out = pg.evaluate("(cases) => import('/js/api.js').then(m => cases.map(c => m.safeNext(c)))", NEXT_CASES)
    ok = {"app.html", "app.html?p=1&q=2", "report.html?p=3"}
    assert set(out) <= ok, dict(zip(NEXT_CASES, out, strict=True))
    assert out[NEXT_CASES.index("app.html?p=1&q=2")] == "app.html?p=1&q=2" and out[NEXT_CASES.index("//evil.example/x")] == "app.html"


@pytest.mark.parametrize("nxt", ["//evil.example/pwn", "https://evil.example/", "javascript:window.__xss=1", "%2F%2Fevil.example"])
def test_login_never_leaves_the_origin_via_next(hpage, nxt):
    pg = hpage()
    pg.goto(f"{pg.base}/auth.html?next={nxt}")
    pg.click("#tab-login")
    pg.fill("#li-email", "a@example.org")
    pg.fill("#li-pass", "correct horse battery")
    pg.click("#login-submit")
    pg.wait_for_url(lambda u: "auth.html" not in u, timeout=10000)
    assert pg.url.startswith(pg.base + "/") and pg.evaluate("window.__xss") is None


@pytest.mark.parametrize("key", ["verify", "reset"])
def test_emailed_tokens_leave_the_url_and_never_travel_in_a_url_or_referer(hpage, key):
    token = "tok_" + "A1b2C3d4" * 5
    api = MockApi(signed_in=False)
    pg = hpage(api=api)
    pg.goto(f"{pg.base}/auth.html?{key}={token}")
    pg.wait_for_function("() => !location.search.includes('tok_')")
    assert token not in pg.url and pg.evaluate("document.referrer") == ""
    assert pg.evaluate("history.length") <= 2
    later = [(m, u, h) for m, u, h in pg.sent[1:]]
    assert all(token not in u for _, u, _ in later)
    assert all("referer" not in {k.lower() for k in h} for _, _, h in later)           # Referrer-Policy: no-referrer is in force
    if key == "reset":
        pg.fill("#rp-pass", "correct horse battery staple")
        pg.click("#reset button[type=submit]")
        pg.wait_for_timeout(300)
        bodies = [b for m, p, b in api.calls if m == "POST" and p == "/auth/reset" and b]
        assert bodies and bodies[0]["token"] == token


# ======================================================================================================== E. CSRF
def test_every_mutating_request_carries_the_csrf_header_from_the_cookie(hpage):
    api = MockApi()
    pg = hpage(api=api)
    pg.goto(f"{pg.base}/account.html")
    pg.wait_for_selector("[data-consent]")
    pg.check("#consent-analytics")
    pg.goto(f"{pg.base}/app.html")
    pg.fill("#np-name", "p1")
    pg.click("#new-project button[type=submit]")
    pg.goto(f"{pg.base}/add.html?p={PID}")
    pg.fill("#smi", "CCO")
    pg.click("#add-submit")
    pg.click("#predict-submit")
    pg.goto(f"{pg.base}/{PROJECT}")
    pg.wait_for_selector("td[data-pred]")
    pg.click("#rank-save")
    pg.click("#logout")
    pg.wait_for_timeout(500)
    mutating = [(m, u, h) for m, u, h in pg.sent if "/api/" in u and m not in ("GET", "HEAD", "OPTIONS")]
    kinds = {(m, re.sub(r"[0-9a-f-]{36}", "ID", u.split("/api")[1].split("?")[0])) for m, u, _ in mutating}
    assert {("POST", "/consents"), ("POST", "/projects"), ("POST", f"/projects/ID/compounds"), ("POST", "/projects/ID/predict"), ("PUT", "/projects/ID/ranking"), ("POST", "/auth/logout")} <= kinds
    for m, u, h in mutating:
        assert h.get("x-csrf-token") == "testtoken", (m, u)
    assert not [1 for m, u, h in pg.sent if m == "GET" and "/api/" in u and "x-csrf-token" in h and "csrf" not in u]


def test_missing_csrf_cookie_is_fetched_first_and_a_stale_one_is_refreshed_once(browser, site):
    from mockapi import install as install_mock

    class Stale(MockApi):
        def __init__(self):
            super().__init__()
            self.n = 0

        def dispatch(self, method, path, body):
            if path == "/consents" and method == "POST":
                self.n += 1
                return 403, {"detail": "csrf_failed"}, {}
            return super().dispatch(method, path, body)

    ctx = browser.new_context()          # no CSRF cookie at all
    try:
        pg = ctx.new_page()
        api = Stale()
        install_mock(pg, api)
        seen = []
        pg.on("request", lambda r: seen.append((r.method, r.url.split("/api")[-1])))
        pg.goto(f"{site.url}/account.html")
        pg.wait_for_selector("[data-consent]")
        pg.check("#consent-analytics")
        pg.wait_for_function("() => document.getElementById('cons-msg').textContent.length > 0")
        assert api.n == 2, "one retry after refreshing the token, never a loop"
        assert seen.index(("GET", "/auth/csrf")) < seen.index(("POST", "/consents"))
        assert "session check failed" in pg.inner_text("#cons-msg")
    finally:
        ctx.close()


# ========================================================================================== F. runtime third parties
def test_no_page_makes_a_third_party_request_with_the_real_api_shapes(hpage):
    for u in ALL_PAGES:
        pg = hpage()
        pg.goto(f"{pg.base}/{u}")
        pg.wait_for_timeout(800)
        if u.startswith("add.html"):
            pg.click("#t-draw")
            pg.wait_for_timeout(3000)
        ext = [r for r in pg.requests if not r.startswith(pg.base) and not r.startswith(("data:", "blob:"))]
        assert not ext, (u, ext)
        assert not [e for e in pg.errors if re.search(r"Content Security Policy|Refused to", e)], (u, pg.errors)


def test_no_csp_violation_on_any_page_under_the_real_header_file(hpage):
    for u in ALL_PAGES:
        pg = hpage()
        pg.goto(f"{pg.base}/{u}")
        pg.wait_for_timeout(800)
        assert not [e for e in pg.errors if re.search(r"Content Security Policy|Refused to", e)], (u, pg.errors)


# =============================================================================================== G. hostile API on all
@pytest.mark.parametrize("payload", PAYLOADS)
@pytest.mark.parametrize("url", ["auth.html", "legal/terms.html"])
def test_hostile_terms_and_auth_answers_are_inert(hpage, url, payload):
    api = PoisonApi(payload, signed_in=False)
    pg = hpage(api=api)
    pg.goto(f"{pg.base}/{url}")
    pg.wait_for_timeout(500)
    assert pg.evaluate("window.__xss") is None and pg.evaluate("document.querySelectorAll('img[src=\"x\"], [onerror]').length") == 0
