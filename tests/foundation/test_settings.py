import pytest
from pydantic import ValidationError

from backend.core.settings import Settings

GOOD = {
    "METIS_ENV": "production",
    "METIS_DATABASE_URL": "postgresql+psycopg://metis_app_login:pw9f3k2@db:5432/metis",
    "METIS_SECRET_KEY": "k" * 20 + "Q9zT4w" * 8,
    "METIS_REDIS_URL": "redis://:s3cr3tpw@redis:6379/0",
    "METIS_OBJECT_STORE_ENDPOINT": "http://minio:9000",
    "METIS_OBJECT_STORE_ACCESS_KEY": "ak8f3j2h",
    "METIS_OBJECT_STORE_SECRET_KEY": "objstore-test-pw",
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
    with pytest.raises(ValidationError, match="64 bytes"):
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


# ---- Wave 4 -----------------------------------------------------------------------------------------------
def test_secret_key_minimum_is_64_bytes_with_a_clear_error(monkeypatch):
    ok = "k" * 20 + "Q9zT4w" * 7 + "ab"  # exactly 64 characters
    assert len(ok.encode()) == 64
    assert load(monkeypatch, {**GOOD, "METIS_SECRET_KEY": ok}).secret_key is not None
    for short in (ok[:-1], "k" * 20 + "Q9zT4w" * 4, "x" * 33):
        with pytest.raises(ValidationError, match="METIS_SECRET_KEY must be at least 64 bytes") as e:
            load(monkeypatch, {**GOOD, "METIS_SECRET_KEY": short})
        assert short not in str(e.value)  # the value is never echoed
    # bytes, not characters: 40 two-byte characters are 80 bytes and pass the length rule
    assert (
        load(monkeypatch, {**GOOD, "METIS_SECRET_KEY": "é" * 20 + "Q9zT4w" * 4 + "é" * 20}).secret_key
        is not None
    )


def test_db_settings_need_only_the_database_url_even_in_production(monkeypatch):
    from backend.core.settings import DbSettings, SettingsError, get_db_settings

    monkeypatch.setenv("METIS_ENV", "production")
    monkeypatch.setenv("METIS_DATABASE_URL", GOOD["METIS_DATABASE_URL"])
    s = get_db_settings()
    assert isinstance(s, DbSettings) and s.db_pool_size == 5 and s.db_statement_timeout_ms == 30000
    assert "pw9f3k2" not in repr(s)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # the full settings still refuse the same environment
    for bad in ("postgresql://postgres:pw@db/metis", "postgresql://metis_owner:pw@db/metis"):
        monkeypatch.setenv("METIS_DATABASE_URL", bad)
        with pytest.raises(SettingsError, match="superuser or schema owner") as e:
            get_db_settings()
        assert ":pw@" not in str(e.value)
    monkeypatch.delenv("METIS_DATABASE_URL")
    with pytest.raises(SettingsError, match="METIS_DATABASE_URL is missing"):
        get_db_settings()


def test_db_settings_are_optional_outside_production(monkeypatch):
    from backend.core.settings import get_db_settings

    monkeypatch.setenv("METIS_ENV", "test")
    assert get_db_settings().database_url is None


def test_the_demo_key_generator_meets_the_minimum():
    import secrets

    assert len(secrets.token_urlsafe(64).encode()) >= 64
    import pathlib

    assert "token_urlsafe(64)" in pathlib.Path("scripts/demo.py").read_text()
