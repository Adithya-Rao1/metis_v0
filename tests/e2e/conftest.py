"""Browser end-to-end fixtures: a real stack (pgserver Postgres with every migration, accounts + domain API + docking
prep from backend.main.create_app, an in-memory mailer, a SYNTHETIC model registry) behind one origin with web/ served
statically (see site.py). Chromium comes from Playwright; one browser per process (shared with tests/web)."""

from __future__ import annotations

import glob
import os
import pathlib
import sys

import psycopg
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "tests" / "e2e", ROOT / "tests" / "dockprep", ROOT / "tests" / "ml"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

os.environ.setdefault("METIS_SECRET_KEY", "test-only-secret-key-0123456789abcdefghij")
os.environ["METIS_ENV"] = "test"

from onesite import LiveServer, Site  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from synth_registry import build_registry  # noqa: E402

from backend.accounts.config import AccountsConfig  # noqa: E402
from backend.accounts.mailer import InMemoryMailer  # noqa: E402
from backend.core.db import make_engine, make_session_factory  # noqa: E402
from backend.core.migrate import apply_migrations  # noqa: E402
from backend.core.settings import get_settings  # noqa: E402
from backend.jobs import deps  # noqa: E402
from backend.jobs.queue import InProcessJobQueue  # noqa: E402
from backend.main import create_app  # noqa: E402
from ml.registry import sync_db  # noqa: E402
from ml.registry.registry import FileModelRegistry  # noqa: E402

WEB = ROOT / "web"
PW = "correct horse battery staple"
TERMS = "2026-01"


def _chromium():
    env = os.environ.get("METIS_CHROMIUM")
    if env:
        return env
    pats = glob.glob(
        os.path.expanduser("~/Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")
    ) + glob.glob(
        os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")
    )
    return sorted(pats)[-1] if pats else None


@pytest.fixture(scope="session")
def browser(request):
    shared = getattr(request.config, "_metis_browser", None)
    if shared is not None:
        yield shared
        return
    with sync_playwright() as p:
        b = p.chromium.launch(
            executable_path=_chromium(),
            args=[
                "--use-gl=angle",
                "--use-angle=swiftshader",
                "--enable-unsafe-swiftshader",
                "--ignore-gpu-blocklist",
            ],
        )
        request.config._metis_browser = b
        try:
            yield b
        finally:
            request.config._metis_browser = None
            b.close()


@pytest.fixture(scope="session")
def pg_uri(tmp_path_factory):
    ext = os.environ.get("METIS_TEST_PG_URI")
    if ext:
        uri, srv = ext, None
    else:
        import pgserver

        srv = pgserver.get_server(tmp_path_factory.mktemp("pg_e2e"), cleanup_mode="stop")
        uri = srv.get_uri()
    apply_migrations(uri)
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS e2e_app_login")
        c.execute("CREATE ROLE e2e_app_login LOGIN PASSWORD 'test-only' IN ROLE metis_app")
    yield uri
    if srv:
        srv.cleanup()


@pytest.fixture(scope="session")
def registry_root(tmp_path_factory):
    return build_registry(tmp_path_factory.mktemp("synthetic_registry"))


class Stack:
    """Handles to the running stack for tests: base url, mailer outbox, superuser DB, the API app."""

    def __init__(self, server, app, pg_uri):
        self.server, self.app, self.pg_uri = server, app, pg_uri
        self.url = server.url
        self.mailer = app.state.mailer

    def token(self, kind: str, email: str) -> str:
        return [t for k, e, t in self.mailer.outbox if k == kind and e == email][-1]

    def sql(self, query: str, *args):
        with psycopg.connect(self.pg_uri, autocommit=True) as c:
            cur = c.execute(query, args)
            return cur.fetchall() if cur.description else []


@pytest.fixture(scope="session")
def stack(pg_uri, registry_root):
    # prediction.model_version_id has an FK to model_version: write the synthetic models' rows (migration role = superuser here).
    sync_db.sync(pg_uri, registry_root, manifest_path=registry_root / "datasets.yaml")
    u = make_url(pg_uri.replace("postgresql://", "postgresql+psycopg://", 1))
    url = u.set(username="e2e_app_login", password="test-only").render_as_string(hide_password=False)
    get_settings.cache_clear()
    engine = make_engine(url)
    factory = make_session_factory(engine)
    app = create_app()
    app.state.session_factory = factory
    # Plain http on 127.0.0.1: cookies cannot be Secure. IP limits are lifted (every test shares 127.0.0.1); per-address limits stay.
    app.state.accounts_config = AccountsConfig(
        bcrypt_cost=4,
        terms_version=TERMS,
        cookie_secure=False,
        rl_register_ip=(10_000, 3600),
        rl_login_ip=(10_000, 300),
        rl_verify_ip=(10_000, 900),
        rl_resend_ip=(10_000, 3600),
        rl_forgot_ip=(10_000, 3600),
        rl_reset_ip=(10_000, 900),
    )
    app.state.mailer = InMemoryMailer()
    queue = InProcessJobQueue(max_workers=2)
    registry = FileModelRegistry(registry_root)
    app.dependency_overrides[deps.get_factory] = lambda: factory
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_queue] = lambda: queue
    server = LiveServer(Site(app, WEB)).start()
    yield Stack(server, app, pg_uri)
    server.stop()
    queue.shutdown()
    engine.dispose()
    get_settings.cache_clear()


@pytest.fixture()
def make_page(browser, stack):
    ctxs = []

    def _make(width=1440, height=900, **kw):
        ctx = browser.new_context(viewport={"width": width, "height": height}, **kw)
        ctxs.append(ctx)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.errors = errors
        page.base = stack.url
        return page

    yield _make
    for c in ctxs:
        c.close()
