"""Maintenance tests: real Postgres (pgserver), all migrations, least-privilege app login role."""

from __future__ import annotations

import os

import psycopg
import pytest
from sqlalchemy.engine import make_url

os.environ.setdefault("METIS_SECRET_KEY", "test-only-secret-key-0123456789abcdefghij")
os.environ["METIS_ENV"] = "test"

from backend.core.db import make_engine, make_session_factory  # noqa: E402
from backend.core.migrate import apply_migrations  # noqa: E402


@pytest.fixture(scope="session")
def pg(tmp_path_factory):
    external = os.environ.get("METIS_TEST_PG_URI")
    if external:
        uri, srv = external, None
    else:
        import pgserver

        srv = pgserver.get_server(tmp_path_factory.mktemp("pgm"), cleanup_mode="stop")
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
    engine = make_engine(app_url)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def su(pg):
    with psycopg.connect(pg, autocommit=True) as c:
        yield c
        c.execute("TRUNCATE app_user, audit_log, rate_limit_bucket CASCADE")
