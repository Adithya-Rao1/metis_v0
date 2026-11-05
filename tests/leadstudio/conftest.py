"""Lead Studio API fixtures: real Postgres (pgserver), all migrations, least-privilege login role, two or three users.

Authentication is replaced ONLY by overriding `current_user` with a header lookup (X-Test-User), the same technique as
tests/api; the genuine session/CSRF/terms path is exercised in tests/security/test_lead_authz.py. Mock mode is forced
(STUDIO_FORCE_MOCK semantics) so nothing needs a GPU, a model download or the network.
"""
from __future__ import annotations

import os

import psycopg
import pytest
from fastapi import Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

os.environ.setdefault("METIS_SECRET_KEY", "test-only-secret-key-0123456789abcdefghij")
os.environ["METIS_ENV"] = "test"

from backend.core.db import make_engine, make_session_factory  # noqa: E402
from backend.core.migrate import apply_migrations  # noqa: E402
from backend.core.settings import get_settings  # noqa: E402
from backend.jobs import deps  # noqa: E402
from backend.main import create_app  # noqa: E402
from backend.schemas.interfaces import UserCtx  # noqa: E402
from leadstudio.server.core import Settings  # noqa: E402
from leadstudio.server.state import build_state  # noqa: E402


def H(who: str) -> dict:
    return {"X-Test-User": who}


@pytest.fixture(scope="session")
def pg(tmp_path_factory):
    external = os.environ.get("METIS_TEST_PG_URI")
    if external:
        uri, srv = external, None
    else:
        import pgserver

        srv = pgserver.get_server(tmp_path_factory.mktemp("pgl"), cleanup_mode="stop")
        uri = srv.get_uri()
    apply_migrations(uri)
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS metis_app_login")
        c.execute("CREATE ROLE metis_app_login LOGIN PASSWORD 'test-only' IN ROLE metis_app")
    yield uri
    if srv:
        srv.cleanup()


@pytest.fixture(scope="session")
def factory(pg):
    u = make_url(pg.replace("postgresql://", "postgresql+psycopg://", 1))
    url = u.set(username="metis_app_login", password="test-only").render_as_string(hide_password=False)
    get_settings.cache_clear()
    engine = make_engine(url)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def su(pg):
    with psycopg.connect(pg, autocommit=True) as c:
        yield c
        c.execute("TRUNCATE app_user CASCADE")
        c.execute("TRUNCATE rate_limit_bucket")


def mk_user(su, email: str) -> UserCtx:
    uid = su.execute(
        "INSERT INTO app_user (email_lower, password_hash, tier, email_verified_at) VALUES (%s, %s, 'free', now()) RETURNING id",
        (email, "$2b$12$" + "a" * 53),
    ).fetchone()[0]
    return UserCtx(id=uid, tier="free", email_verified=True)


@pytest.fixture
def users(su):
    return {"a": mk_user(su, "a@example.org"), "b": mk_user(su, "b@example.org"), "c": mk_user(su, "c@example.org")}


def make_app(factory, users, settings: Settings | None = None, **state_kw):
    app = create_app()
    app.state.lead = build_state(
        settings=settings or Settings(force_mock=True, mock_delay=0.0, replay_delay=0.0), **state_kw)

    def fake_current_user(x_test_user: str | None = Header(default=None)) -> UserCtx:
        if x_test_user not in users:
            raise HTTPException(status_code=401, detail="not_authenticated")
        return users[x_test_user]

    app.dependency_overrides[deps.current_user] = fake_current_user
    app.dependency_overrides[deps.get_factory] = lambda: factory
    return app


@pytest.fixture
def app(factory, users):
    return make_app(factory, users)


@pytest.fixture
def client(app):
    return TestClient(app)
