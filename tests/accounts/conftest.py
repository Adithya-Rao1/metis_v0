import os
import time
import uuid

import jwt
import psycopg
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

os.environ.setdefault("METIS_SECRET_KEY", "test-only-secret-key-0123456789abcdefghij")
os.environ["METIS_ENV"] = "test"

from backend.accounts import router  # noqa: E402
from backend.accounts.config import COOKIE_CSRF, CSRF_HEADER, AccountsConfig, jwt_key  # noqa: E402
from backend.accounts.deps import current_user  # noqa: E402
from backend.accounts.mailer import InMemoryMailer  # noqa: E402
from backend.core.db import make_engine, make_session_factory  # noqa: E402
from backend.core.migrate import apply_migrations  # noqa: E402
from backend.core.settings import get_settings  # noqa: E402

PW = "correct horse battery"
TERMS = "2026-01"


@pytest.fixture(scope="session")
def pg(tmp_path_factory):
    external = os.environ.get("METIS_TEST_PG_URI")
    if external:
        uri, srv = external, None
    else:
        import pgserver

        srv = pgserver.get_server(tmp_path_factory.mktemp("pga"), cleanup_mode="stop")
        uri = srv.get_uri()
    apply_migrations(uri)
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS metis_app_login")
        c.execute("CREATE ROLE metis_app_login LOGIN PASSWORD 'test-only' IN ROLE metis_app")
    yield uri
    if srv:
        srv.cleanup()


@pytest.fixture(scope="session")
def app_url(pg):
    u = make_url(pg.replace("postgresql://", "postgresql+psycopg://", 1))
    return u.set(username="metis_app_login", password="test-only").render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def factory(app_url):
    get_settings.cache_clear()
    return make_session_factory(make_engine(app_url))


@pytest.fixture
def su(pg):
    with psycopg.connect(pg, autocommit=True) as c:
        yield c
        c.execute("TRUNCATE app_user, deletion_request, audit_log, rate_limit_bucket, model_version CASCADE")


@pytest.fixture
def cfg():
    return AccountsConfig(bcrypt_cost=4, terms_version=TERMS, cookie_secure=True)


@pytest.fixture
def app(factory, cfg, su):
    a = FastAPI()
    a.include_router(router)
    a.state.session_factory = factory
    a.state.accounts_config = cfg
    a.state.mailer = InMemoryMailer()

    user_dep = Depends(current_user)

    @a.get("/_whoami")
    def whoami(u=user_dep):
        return {"id": str(u.id), "tier": u.tier}

    @a.post("/_mutate")
    def mutate(u=user_dep):
        return {"ok": True}

    return a


class C:
    """Test client wrapper that handles the CSRF double-submit."""

    def __init__(self, app):
        self.app = app
        self.http = TestClient(app, base_url="https://testserver")
        self.mailer = app.state.mailer

    def csrf(self):
        if COOKIE_CSRF not in self.http.cookies:
            self.http.get("/auth/csrf")
        return self.http.cookies.get(COOKIE_CSRF)

    def h(self):
        return {CSRF_HEADER: self.csrf()}

    def post(self, url, json=None, csrf=True):
        return self.http.post(url, json=json, headers=self.h() if csrf else {})

    def delete(self, url, json=None):
        return self.http.request("DELETE", url, json=json, headers=self.h())

    def get(self, url):
        return self.http.get(url)

    def register(self, email="a@example.com", pw=PW, version=TERMS):
        return self.post(
            "/auth/register", {"email": email, "password": pw, "accepted_terms_version": version}
        )

    def token_for(self, email):
        return [t for k, e, t in self.mailer.outbox if k == "verify" and e == email][-1]

    def verify(self, email):
        return self.post("/auth/verify", {"token": self.token_for(email)})

    def login(self, email="a@example.com", pw=PW):
        return self.post("/auth/login", {"email": email, "password": pw})

    def signup_login(self, email="a@example.com", pw=PW):
        assert self.register(email, pw).status_code == 202
        assert self.verify(email).status_code == 200
        r = self.login(email, pw)
        assert r.status_code == 200, r.text
        return uuid.UUID(r.json()["user"]["id"])


@pytest.fixture
def client(app):
    return C(app)


@pytest.fixture
def client2(app):
    return C(app)


@pytest.fixture
def forge():
    def _forge(claims_over=None, key=None, alg="HS256", drop=()):
        now = int(time.time())
        claims = {
            "sub": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + 600,
            "iss": "metis",
        }
        claims.update(claims_over or {})
        for d in drop:
            claims.pop(d)
        return jwt.encode(claims, key if key is not None else jwt_key(), algorithm=alg)

    return _forge
