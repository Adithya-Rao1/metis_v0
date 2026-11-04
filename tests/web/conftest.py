"""Shared fixtures for web tests: a static server over web/ and headless Chromium via Playwright.

Browser: Playwright for Python (venv at .venv-web) driving the cached headless Chromium shell.
Set METIS_CHROMIUM to override the executable path.
"""
import functools
import glob
import http.server
import os
import threading
from pathlib import Path

import pytest
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
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=_chromium(), args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        yield b
        b.close()


@pytest.fixture()
def make_page(browser, base_url):
    ctxs = []

    def _make(width=1440, height=900, **kw):
        ctx = browser.new_context(viewport={"width": width, "height": height}, **kw)
        ctxs.append(ctx)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.errors = errors
        page.base = base_url
        return page

    yield _make
    for c in ctxs:
        c.close()
