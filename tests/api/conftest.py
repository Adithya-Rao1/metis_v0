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

MODEL_VERSION = uuid.UUID("11111111-1111-1111-1111-111111111111")
CARDS = {
    # test-only model cards; nothing here is a real model
    "logd": dict(kind="regression", units="log units", default_range=(-2.0, 6.0), mw=1.0),
    "sol": dict(kind="regression", units="log mol/L", default_range=(-8.0, 0.0), mw=2.0),
    "ames": dict(kind="classification", units=None, default_range=(0.0, 1.0), mw=None),
}


class FakeRegistry:
    """In-test registry. Values are derived from a hash so they are deterministic and obviously synthetic."""

    version_id = MODEL_VERSION

    def _card(self, e: str) -> ModelCard:
        if e not in CARDS:
            raise KeyError(e)
        c = CARDS[e]
        bench = {"test_only": True}
        if c["mw"]:
            bench["median_calibration_width"] = c["mw"]
        return ModelCard(
            endpoint=e, version="test-0", kind=c["kind"], units=c["units"],
            datasets=[{"name": "test", "url": "https://example.invalid", "license": "test", "size": 1, "split": "scaffold"}],
            limitations="Test model for unit tests only.", split_type="scaffold", benchmark=bench,
            coverage_by_n={"3": 0.9, "5": 0.9, "10": 0.9, "20": 0.9}, runtime_by_n_seconds={"3": 1, "5": 2, "10": 4, "20": 8},
            default_range=c["default_range"], weights_sha256="0" * 64,
        )

    def list_models(self):
        return [self._card(e) for e in CARDS]

    def model_card(self, endpoint):
        return self._card(endpoint)

    def time_estimate_seconds(self, endpoint, ensemble_n, n_compounds):
        self._card(endpoint)
        return 0.5 * ensemble_n * n_compounds

    def predict(self, endpoint, canonical_smiles, ensemble_n):
        card = self._card(endpoint)
        h = int(hashlib.sha256(f"{endpoint}{canonical_smiles}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        lo_r, hi_r = card.default_range
        bd = UncertaintyBreakdown(ensemble_disagreement=0.5, similarity_to_training=0.3, assay_noise=0.2)
        if card.kind == "regression":
            v = lo_r + h * (hi_r - lo_r)
            p = Prediction(endpoint=endpoint, kind="regression", model_version_id=self.version_id, ensemble_n=ensemble_n, value=v,
                           interval_low=v - 0.5, interval_high=v + 0.5, coverage=0.9, sample_size=1000, method_note="test", breakdown=bd)
        else:
            p = Prediction(endpoint=endpoint, kind="classification", model_version_id=self.version_id, ensemble_n=ensemble_n, value=h,
                           probability=h, coverage=0.9, sample_size=1000, method_note="test", breakdown=bd)
        return PredictionDetail(prediction=p, fragments=[FragmentShare(atom_indices=[0], share=0.5, share_low=0.4, share_high=0.6, train_frequency=0.1)],
                                nearest_training=[{"smiles": "CC", "similarity": 0.5, "label": 1.0}], runtime_seconds=0.1)


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
def queue():
    return InProcessJobQueue(max_workers=2)


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


def H(who: str) -> dict:
    return {"X-Test-User": who}


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
