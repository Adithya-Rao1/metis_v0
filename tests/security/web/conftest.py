"""Browser fixtures for the Wave 1 web red-team checks (Playwright; skipped when it is not installed).

Run with the Playwright venv, cutting off the parent conftest (it needs pgserver/psycopg):
  /Users/adithyarao/.superset/worktrees/metis_v0/agent3-design/.venv-web/bin/python -m pytest \
      tests/security/web --confcutdir=tests/security/web -q
Python 3.9 compatible on purpose (that venv is 3.9).
"""
from __future__ import annotations

import functools
import glob
import http.server
import os
import threading
from pathlib import Path

import pytest

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # normal (non-Playwright) venv: skip this directory instead of failing collection
    sync_playwright = None
    collect_ignore_glob = ["test_*.py"]

ROOT = Path(__file__).resolve().parents[3]
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
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(WEB)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


@pytest.fixture(scope="session")
def browser():
    exe = _chromium()
    if not exe or sync_playwright is None:
        pytest.skip("no headless chromium available")
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=exe, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        yield b
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
