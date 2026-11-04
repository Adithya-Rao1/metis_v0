"""Browser fixtures for the Wave 1 web red-team checks (Playwright; skipped when it is not installed).

Run: python -m pytest tests/web tests/security/web -q   (needs `pip install playwright` and a Playwright chromium).
Shares one Playwright instance with tests/web (see the `browser` fixture).
"""
from __future__ import annotations

import functools
import glob
import http.server
import os
import threading
from pathlib import Path

import sys

import pytest

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # normal (non-Playwright) venv: skip this directory instead of failing collection
    sync_playwright = None
    collect_ignore_glob = ["test_*.py"]

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "web"
for _p in (ROOT, ROOT / "tests" / "web"):  # MockApi (test-only API stand-in) lives in tests/web
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


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
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(WEB)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


@pytest.fixture(scope="session")
def browser(request):
    # One Playwright per process: tests/web and tests/security/web share it (a second sync_playwright() fails
    # while the first is open). Whichever fixture runs first creates it and closes it.
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


@pytest.fixture()
def make_page(browser, base_url):
    ctxs = []

    def _make(width=1440, height=900, **kw):
        for c in ctxs:  # one live context at a time: swiftshader WebGL contexts are scarce
            c.close()
        ctxs.clear()
        ctx = browser.new_context(viewport={"width": width, "height": height}, **kw)
        ctxs.append(ctx)
        page = ctx.new_page()
        page.errors = []
        page.requests = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.on("request", lambda r: page.requests.append(r.url))
        page.base = base_url
        return page

    yield _make
    for c in ctxs:
        c.close()


@pytest.fixture()
def api_page(browser, base_url):
    """Like make_page, but a signed-in MockApi (tests/web/mockapi.py, test-only data) answers /api/** and a CSRF cookie is
    present. `api` may be a MockApi subclass instance (used to serve hostile strings). Records requests and page errors."""
    from mockapi import MockApi
    from mockapi import install as install_mock

    ctxs = []

    def _make(width=1440, height=900, api="default", **kw):
        for c in ctxs:
            c.close()
        ctxs.clear()
        ctx = browser.new_context(viewport={"width": width, "height": height}, **kw)
        ctxs.append(ctx)
        ctx.add_cookies([{"name": "metis_csrf", "value": "testtoken", "url": base_url}])
        page = ctx.new_page()
        page.errors = []
        page.requests = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
        page.on("request", lambda r: page.requests.append(r.url))
        if api == "default":
            api = MockApi()
        if api is not None:
            install_mock(page, api)
        page.base = base_url
        return page

    yield _make
    for c in ctxs:
        c.close()
