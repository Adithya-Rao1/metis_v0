"""Shared fixtures for web tests: a static server over web/ and headless Chromium via Playwright.

Browser: Playwright for Python (venv at .venv-web) driving the cached headless Chromium shell.
Set METIS_CHROMIUM to override the executable path.
"""
import functools
import glob
import http.server
import os
import sys
import threading
from pathlib import Path

import pytest
from mockapi import MockApi
from mockapi import install as install_mock
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def _chromium():
    env = os.environ.get("METIS_CHROMIUM")
    if env:
        return env
    pats = glob.glob(os.path.expanduser("~/Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")) + \
        glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
    return sorted(pats)[-1] if pats else None


@pytest.fixture(scope="session")
def base_url():
    handler = functools.partial(_Quiet, directory=str(WEB))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="session")
def browser(request):
    # One Playwright per process: tests/web and tests/security/web share it (a second sync_playwright() fails
    # while the first is open). Whichever fixture runs first creates it and closes it.
    shared = getattr(request.config, "_metis_browser", None)
    if shared is not None:
        yield shared
        return

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=_chromium(), args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        request.config._metis_browser = b
        try:
            yield b
        finally:
            request.config._metis_browser = None
            b.close()


@pytest.fixture()
def make_page(browser, base_url):
    ctxs = []

    def _make(width=1440, height=900, api="default", **kw):
        """`api`: a MockApi (default: a signed-in one with models, a project and predictions), or None for no API at all."""
        ctx = browser.new_context(viewport={"width": width, "height": height}, **kw)
        ctxs.append(ctx)
        ctx.add_cookies([{"name": "metis_csrf", "value": "testtoken", "url": base_url}])
        page = ctx.new_page()
        if api == "default":
            api = MockApi()
        if api is not None:
            install_mock(page, api)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.errors = errors
        page.base = base_url
        return page

    yield _make
    for c in ctxs:
        c.close()


# ---- a site that applies web/_headers (so the browser enforces the real CSP and the iframe sandbox) ----
sys.path.insert(0, str(ROOT / "tests" / "e2e"))


@pytest.fixture(scope="session")
def hsite():
    """web/ served with web/_headers applied; /api answers 404 unless a test intercepts it (tests/e2e/onesite.py)."""
    from fastapi import FastAPI
    from onesite import LiveServer, Site

    srv = LiveServer(Site(FastAPI(docs_url=None, openapi_url=None), WEB)).start()
    yield srv
    srv.stop()


@pytest.fixture()
def make_hpage(browser, hsite):
    ctxs = []

    def _make(width=1440, height=900, api="default", **kw):
        ctx = browser.new_context(viewport={"width": width, "height": height}, **kw)
        ctxs.append(ctx)
        ctx.add_cookies([{"name": "metis_csrf", "value": "testtoken", "url": hsite.url}])
        page = ctx.new_page()
        if api == "default":
            api = MockApi()
        if api is not None:
            install_mock(page, api)
        page.errors, page.requests = [], []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
        page.on("request", lambda r: page.requests.append(r.url))
        page.base = hsite.url
        return page

    yield _make
    for c in ctxs:
        c.close()
