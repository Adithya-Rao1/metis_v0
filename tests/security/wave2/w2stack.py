"""Helpers for the Wave 2 red-team suite: a real end-to-end stack (accounts + domain API + dockprep) on pgserver.

Nothing here is production code. The pgserver instance comes from tests/security/conftest.py (`pg`).
"""
from __future__ import annotations

import os
import pathlib
import sys
import uuid

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

ROOT = pathlib.Path(__file__).resolve().parents[3]
for p in (ROOT, ROOT / "tests" / "api", ROOT / "tests" / "ml", ROOT / "tests" / "dockprep"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

os.environ.setdefault("METIS_SECRET_KEY", "test-only-secret-key-0123456789abcdefghij")
os.environ["METIS_ENV"] = "test"

from backend.accounts.config import COOKIE_CSRF, CSRF_HEADER, AccountsConfig  # noqa: E402
from backend.accounts.mailer import InMemoryMailer  # noqa: E402
from backend.core.db import make_engine, make_session_factory  # noqa: E402
from backend.core.settings import get_settings  # noqa: E402
from backend.jobs import deps  # noqa: E402
from backend.jobs.queue import InProcessJobQueue  # noqa: E402
from backend.main import create_app  # noqa: E402

PW = "correct horse battery"
TERMS = "2026-01"


def login_factory(pg_uri: str, role: str = "w2_app_login"):
    with psycopg.connect(pg_uri, autocommit=True) as c:
        c.execute(f"DROP ROLE IF EXISTS {role}")
        c.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'test-only' IN ROLE metis_app")
    u = make_url(pg_uri.replace("postgresql://", "postgresql+psycopg://", 1))
    url = u.set(username=role, password="test-only").render_as_string(hide_password=False)
    get_settings.cache_clear()
    engine = make_engine(url)
    return make_session_factory(engine), engine


def build_app(factory, registry=None, queue=None, cfg: AccountsConfig | None = None, terms: str = TERMS):
    app = create_app()
    app.state.session_factory = factory
    app.state.accounts_config = cfg or AccountsConfig(bcrypt_cost=4, terms_version=terms, cookie_secure=True)
    app.state.mailer = InMemoryMailer()
    app.dependency_overrides[deps.get_factory] = lambda: factory
    if registry is not None:
        app.dependency_overrides[deps.get_registry] = lambda: registry
    q = queue or InProcessJobQueue(max_workers=2)
    app.dependency_overrides[deps.get_queue] = lambda: q
    app.state.test_queue = q
    return app


class Cl:
    """Browser-like client: cookie jar + CSRF double-submit."""

    def __init__(self, app, base="https://testserver"):
        self.app = app
        self.http = TestClient(app, base_url=base)
        self.mailer = app.state.mailer
        self.uid: uuid.UUID | None = None

    def csrf(self):
        if COOKIE_CSRF not in self.http.cookies:
            self.http.get("/auth/csrf")
        return self.http.cookies.get(COOKIE_CSRF)

    def h(self, extra=None):
        return {CSRF_HEADER: self.csrf(), **(extra or {})}

    def req(self, method, url, **kw):
        headers = self.h(kw.pop("headers", None))
        return self.http.request(method, url, headers=headers, **kw)

    def get(self, url, **kw):
        return self.http.get(url, **kw)

    def post(self, url, json=None, **kw):
        return self.req("POST", url, json=json, **kw)

    def put(self, url, json=None, **kw):
        return self.req("PUT", url, json=json, **kw)

    def delete(self, url, json=None, **kw):
        return self.req("DELETE", url, json=json, **kw)

    def register(self, email, pw=PW, version=TERMS):
        return self.post("/auth/register", {"email": email, "password": pw, "accepted_terms_version": version})

    def token_for(self, email):
        return [t for k, e, t in self.mailer.outbox if k == "verify" and e == email][-1]

    def verify(self, email):
        return self.post("/auth/verify", {"token": self.token_for(email)})

    def login(self, email, pw=PW):
        return self.post("/auth/login", {"email": email, "password": pw})

    def signup(self, email, pw=PW):
        assert self.register(email, pw).status_code == 202
        assert self.verify(email).status_code == 200
        r = self.login(email, pw)
        assert r.status_code == 200, r.text
        self.uid = uuid.UUID(r.json()["user"]["id"])
        return self.uid
