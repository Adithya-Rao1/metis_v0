"""Fixtures for the Wave 3 red-team suite (Agent 10): real accounts + domain API on pgserver, and a browser site that applies
web/_headers. Everything here is test-only. The pgserver instance (`pg`) comes from tests/security/conftest.py."""
from __future__ import annotations

import pathlib
import sys

import psycopg
import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (HERE, ROOT, ROOT / "tests" / "security" / "wave2", ROOT / "tests" / "api", ROOT / "tests" / "web", ROOT / "tests" / "security" / "web", ROOT / "tests" / "e2e"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import w2stack as S  # noqa: E402
from api_support import FakeRegistry  # noqa: E402

from backend.accounts.config import AccountsConfig  # noqa: E402

from w3util import HI  # noqa: E402


@pytest.fixture(scope="module")
def w3(pg):
    factory, engine = S.login_factory(pg, role="w3_app_login")
    yield factory
    engine.dispose()


@pytest.fixture
def su3(pg):
    with psycopg.connect(pg, autocommit=True) as c:
        yield c
        c.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_no_update")
        c.execute("TRUNCATE app_user, model_version, deletion_request, audit_log, rate_limit_bucket, session_revocation CASCADE")
        c.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_no_update")


@pytest.fixture
def mk3(w3, su3):
    """Factory: an app with generous rate limits unless cfg overrides them. Fake registry (test-only data)."""

    def make(queue=None, registry=None, **cfg):
        su3.execute("INSERT INTO model_version (id, endpoint, version) VALUES (%s,'test','t0') ON CONFLICT DO NOTHING", (str(FakeRegistry.version_id),))
        return S.build_app(w3, registry or FakeRegistry(), queue, AccountsConfig(**{**HI, **cfg}))

    return make


# ------------------------------------------------------------------------------------------------ browser
import glob  # noqa: E402
import os  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


def _chromium():
    env = os.environ.get("METIS_CHROMIUM")
    if env:
        return env
    pats = glob.glob(os.path.expanduser("~/Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")) + \
        glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
    return sorted(pats)[-1] if pats else None


@pytest.fixture(scope="session")
def browser(request):
    # One Playwright per process (shared with tests/web and tests/security/web through request.config._metis_browser).
    shared = getattr(request.config, "_metis_browser", None)
    if shared is not None:
        yield shared
        return
    exe = _chromium()
    if not exe or sync_playwright is None:
        pytest.skip("no headless chromium available")
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=exe, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        request.config._metis_browser = b
        try:
            yield b
        finally:
            request.config._metis_browser = None
            b.close()


@pytest.fixture(scope="session")
def site():
    """web/ served by the one-origin test site WITH web/_headers applied (so the browser enforces the real CSP);
    /api answers 404 from an empty FastAPI app unless a test intercepts it."""
    from fastapi import FastAPI
    from onesite import LiveServer, Site

    srv = LiveServer(Site(FastAPI(docs_url=None, openapi_url=None), ROOT / "web")).start()
    yield srv
    srv.stop()


@pytest.fixture()
def hpage(browser, site):
    """A page on the header-applying site; `api=` installs a MockApi (tests/web/mockapi.py, test-only data)."""
    from mockapi import MockApi
    from mockapi import install as install_mock

    ctxs = []

    def _make(api="default", width=1440, height=900, **kw):
        for c in ctxs:      # one live context at a time: swiftshader WebGL contexts are scarce
            c.close()
        ctxs.clear()
        ctx = browser.new_context(viewport={"width": width, "height": height}, **kw)
        ctxs.append(ctx)
        ctx.add_cookies([{"name": "metis_csrf", "value": "testtoken", "url": site.url}])
        page = ctx.new_page()
        page.errors, page.requests, page.sent = [], [], []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
        page.on("request", lambda r: (page.requests.append(r.url), page.sent.append((r.method, r.url, dict(r.headers)))))
        if api == "default":
            api = MockApi()
        if api is not None:
            install_mock(page, api)
        page.base = site.url
        return page

    yield _make
    for c in ctxs:
        c.close()
