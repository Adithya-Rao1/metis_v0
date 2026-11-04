import pytest
from pydantic import ValidationError

from backend.core.settings import Settings

GOOD = {
    "METIS_ENV": "production",
    "METIS_DATABASE_URL": "postgresql+psycopg://metis_app_login:pw9f3k2@db:5432/metis",
    "METIS_SECRET_KEY": "k" * 40,
    "METIS_REDIS_URL": "redis://:s3cr3tpw@redis:6379/0",
    "METIS_OBJECT_STORE_ENDPOINT": "http://minio:9000",
    "METIS_OBJECT_STORE_ACCESS_KEY": "ak8f3j2h",
    "METIS_OBJECT_STORE_SECRET_KEY": "sk7d9s8a6f5g4h3j",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    import os

    for k in list(os.environ):
        if k.startswith("METIS_"):
            monkeypatch.delenv(k)


def load(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_no_secret_has_a_default(monkeypatch):
    s = load(monkeypatch, {})
    assert s.env == "development"
    assert s.database_url is None and s.secret_key is None and s.object_store_secret_key is None


def test_production_accepts_complete_config(monkeypatch):
    assert load(monkeypatch, GOOD).env == "production"


@pytest.mark.parametrize("missing", [k for k in GOOD if k != "METIS_ENV"])
def test_production_refuses_missing_secret(monkeypatch, missing):
    env = {k: v for k, v in GOOD.items() if k != missing}
    with pytest.raises(ValidationError) as e:
        load(monkeypatch, env)
    assert missing in str(e.value)


def test_production_refuses_placeholder_and_short_key(monkeypatch):
    with pytest.raises(ValidationError, match="placeholder"):
        load(monkeypatch, {**GOOD, "METIS_OBJECT_STORE_SECRET_KEY": "changeme"})
    with pytest.raises(ValidationError, match="32 characters"):
        load(monkeypatch, {**GOOD, "METIS_SECRET_KEY": "short"})


def test_production_refuses_superuser_url(monkeypatch):
    with pytest.raises(ValidationError, match="superuser"):
        load(monkeypatch, {**GOOD, "METIS_DATABASE_URL": "postgresql://postgres:pw@db/metis"})


def test_secrets_not_in_repr_or_error(monkeypatch):
    s = load(monkeypatch, GOOD)
    assert "pw9f3k2" not in repr(s) and "s3cr3tpw" not in str(s)
    with pytest.raises(ValidationError) as e:
        load(
            monkeypatch,
            {**GOOD, "METIS_SECRET_KEY": "short", "METIS_OBJECT_STORE_ACCESS_KEY": "hunter2changeme"},
        )
    assert "hunter2" not in str(e.value) and "pw9f3k2" not in str(e.value)


def test_dev_mode_does_not_require_secrets(monkeypatch):
    assert load(monkeypatch, {"METIS_ENV": "test"}).env == "test"
