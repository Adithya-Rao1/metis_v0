"""Domain API test fixtures: real Postgres (pgserver), all migrations, least-privilege login role, fake users and registry."""

from __future__ import annotations

import hashlib
import os
import uuid

import psycopg
import pytest
from fastapi import Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from backend.core.db import make_engine, make_session_factory
from backend.core.migrate import apply_migrations
from backend.jobs import deps
from backend.jobs.queue import InProcessJobQueue
from backend.main import create_app
from backend.schemas.contracts import Prediction, UncertaintyBreakdown
from backend.schemas.interfaces import FragmentShare, ModelCard, PredictionDetail, UserCtx
from api_support import CARDS, MODEL_VERSION, FakeRegistry, H  # noqa: E402,F401


@pytest.fixture(scope="session")
def pg(tmp_path_factory):
    external = os.environ.get("METIS_TEST_PG_URI")
    if external:
        uri, srv = external, None
    else:
        import pgserver

        srv = pgserver.get_server(tmp_path_factory.mktemp("pg6"), cleanup_mode="stop")
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
    engine = make_engine(url)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def su(pg):
    with psycopg.connect(pg, autocommit=True) as c:
        c.execute("INSERT INTO model_version (id, endpoint, version) VALUES (%s, 'test', 'test-0') ON CONFLICT DO NOTHING", (MODEL_VERSION,))
        yield c
        c.execute("TRUNCATE app_user, model_version CASCADE")


def _mk_user(su, email: str, tier: str = "free") -> UserCtx:
    uid = su.execute(
        "INSERT INTO app_user (email_lower, password_hash, tier, email_verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
        (email, "$2b$12$" + "a" * 53, tier),
    ).fetchone()[0]
    return UserCtx(id=uid, tier=tier, email_verified=True)


@pytest.fixture
def users(su):
    return {"a": _mk_user(su, "a@example.org"), "b": _mk_user(su, "b@example.org"), "edu": _mk_user(su, "e@example.edu", "edu")}


@pytest.fixture
def queue(factory):
    return InProcessJobQueue(max_workers=2, factory=factory)


@pytest.fixture
def app(factory, users, queue):
    application = create_app()
    by_hdr = {k: v for k, v in users.items()}

    def fake_current_user(x_test_user: str | None = Header(default=None)) -> UserCtx:
        if x_test_user not in by_hdr:
            raise HTTPException(status_code=401, detail="unauthenticated")
        return by_hdr[x_test_user]

    application.dependency_overrides[deps.current_user] = fake_current_user
    application.dependency_overrides[deps.get_factory] = lambda: factory
    application.dependency_overrides[deps.get_registry] = lambda: FakeRegistry()
    application.dependency_overrides[deps.get_queue] = lambda: queue
    return application


@pytest.fixture
def client(app):
    return TestClient(app)



@pytest.fixture
def project(client):
    r = client.post("/projects", json={"name": "P1"}, headers=H("a"))
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def loaded(client, project):
    r = client.post(f"/projects/{project}/compounds", json={"format": "smiles", "smiles": ["CCO", "c1ccccc1O", "CC(=O)Nc1ccc(O)cc1"]}, headers=H("a"))
    assert r.status_code == 201, r.text
    return project, r.json()["compounds"]
