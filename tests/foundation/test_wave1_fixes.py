"""Regression tests for Wave 1 QA findings F-01, F-02, F-03, F-04..F-07, F-09, F-10, F-13, F-14, F-15, F-22, F-40."""

import io
import json
import logging
import logging.config
import pathlib
import re
import threading
import uuid

import psycopg
import pytest
import yaml
from psycopg.conninfo import make_conninfo
from sqlalchemy import text

from backend.core.logging import configure_logging, scrub_text
from backend.core.migrate import MigrationError, apply_migrations
from backend.core.settings import Settings, SettingsError, get_settings

ROOT = pathlib.Path(__file__).resolve().parents[2]
ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"


@pytest.fixture
def buf():
    b = io.StringIO()
    configure_logging("DEBUG", stream=b)
    yield b
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_metis", False):
            root.removeHandler(h)


def emit(b, msg, *args, **kw):
    logging.getLogger("w1").info(msg, *args, **kw)
    return b.getvalue()


# ---- F-01 / F-13 ---------------------------------------------------------------------------------------------
def test_f01_pool_reuse_survives_many_requests(app_url):
    from backend.core.db import make_engine, make_session_factory, request_session

    eng = make_engine(app_url, settings=Settings(env="test", db_pool_size=1))
    try:
        f = make_session_factory(eng)
        for _ in range(30):
            with request_session(f, uuid.uuid4()) as s:
                assert s.execute(text("SELECT current_user_id()")).scalar()
    finally:
        eng.dispose()


def test_f01_pool_reuse_survives_many_requests_threaded(app_url):
    from backend.core.db import make_engine, make_session_factory, request_session

    eng = make_engine(app_url, settings=Settings(env="test", db_pool_size=2))
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(25):
                uid = uuid.uuid4()
                with request_session(make_session_factory(eng), uid) as s:
                    assert s.execute(text("SELECT current_user_id()")).scalar() == uid
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    eng.dispose()
    assert not errors, errors[:1]


def test_f13_user_id_canonicalised_or_rejected(app_url):
    from backend.core.db import _canonical_user_id

    u = uuid.uuid4()
    assert _canonical_user_id(str(u).upper()) == str(u)
    assert _canonical_user_id(u) == str(u)
    assert _canonical_user_id(None) == "" and _canonical_user_id("") == ""
    for bad in (f"urn:uuid:{u}", u.hex, f"{{{u}}}", str(u)[:8] + "urn:" + str(u)[8:], "١" * 32, f"{u}\n"):
        with pytest.raises(ValueError):
            _canonical_user_id(bad)
    with pytest.raises(TypeError):
        _canonical_user_id(1)  # type: ignore[arg-type]


# ---- F-02 / F-03 / F-09 / F-22 -------------------------------------------------------------------------------
COMPOSE = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text())
CI_TEXT = (ROOT / ".github/workflows/ci.yml").read_text()
CI = yaml.safe_load(CI_TEXT)


def test_f02_app_gets_only_app_role_variables():
    app = COMPOSE["services"]["app"]
    assert not app.get("env_file"), "app must not load the whole deploy/.env"
    env = app["environment"]
    names = set(env)
    forbidden = {"POSTGRES_PASSWORD", "METIS_MIGRATION_URL", "METIS_APP_DB_PASSWORD", "REDIS_PASSWORD"}
    assert not names & forbidden
    refs = " ".join(str(v) for v in env.values())
    assert not any(f in refs for f in forbidden)
    assert "METIS_DATABASE_URL" in names
    assert "METIS_MIGRATION_URL" in COMPOSE["services"]["migrate"]["environment"]


def test_f03_ci_installs_what_it_collects():
    runs = " ".join(s.get("run", "") for s in CI["jobs"]["test"]["steps"])
    assert "ml/requirements.txt" in runs and "--ignore=tests/web" in runs
    web = " ".join(s.get("run", "") for s in CI["jobs"]["web"]["steps"])
    assert "playwright" in web and "playwright install" in web and "chromium" in web and "tests/web" in web


def test_f22_actions_pinned_to_commit_sha():
    uses = re.findall(r"uses:\s*(\S+)", CI_TEXT)
    assert uses and all(re.search(r"@[0-9a-f]{40}$", u) for u in uses), uses


def test_f09_container_cmd_and_uvicorn_log_config():
    cmd = re.search(r"^CMD\s+(\[.*\])", (ROOT / "deploy/Dockerfile").read_text(), re.M).group(1)
    argv = json.loads(cmd)
    assert "--no-access-log" in argv and "--log-config" in argv
    cfg = json.loads((ROOT / "deploy/uvicorn_log_config.json").read_text())
    assert "backend.core.logging.RedactionFilter" in json.dumps(cfg)
    for name, lg in cfg["loggers"].items():
        assert lg["handlers"] and all("redact" in cfg["handlers"][h]["filters"] for h in lg["handlers"]), name
    stream = io.StringIO()
    cfg["handlers"]["default"]["stream"] = stream
    logging.config.dictConfig(cfg)
    try:
        logging.getLogger("uvicorn.error").warning(
            "GET /x?access_token=abc123 failed for postgresql://u:pw9@db/x"
        )
    finally:
        for n in cfg["loggers"]:
            logging.getLogger(n).handlers = []
            logging.getLogger(n).propagate = True
    out = stream.getvalue()
    assert "abc123" not in out and "pw9" not in out


def test_f09_configure_logging_routes_uvicorn_through_redaction(buf):
    lg = logging.getLogger("uvicorn.access")
    lg.propagate = False
    lg.addHandler(logging.NullHandler())
    configure_logging("INFO", stream=buf)
    assert lg.propagate and not lg.handlers
    lg.warning('%s - "%s %s HTTP/%s" %d', "1.2.3.4", "GET", "/v?token=abc123&smiles=" + ASPIRIN, "1.1", 200)
    out = buf.getvalue()
    assert "abc123" not in out and "Oc1ccccc1" not in out


# ---- F-04 / F-06 / F-07 / F-40 -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "smi",
    [
        "CC(=O)[O-].[Na+]",
        "O=C([O-])c1ccccc1.[K+]",
        "[Na+].[Cl-].c1ccccc1CCN(C)C",
        "[Se]c1ccccc1CCN",
        "C[Mg+2]CC1CC1",
    ],
)
def test_f04_salt_and_heteroatom_smiles_redacted(buf, smi):
    out = emit(buf, "submitted %s", smi)
    assert smi not in out and "c1ccccc1" not in out and "[Na+]" not in out and "[Se]" not in out


def test_f40_smiles_glued_to_prefix_redacted(buf):
    out = emit(buf, f"item mol_{ASPIRIN} done row{ASPIRIN} q={ASPIRIN}")
    assert "Oc1ccccc1" not in out and "item" in out and "done" in out


@pytest.mark.parametrize(
    "extra,msg",
    [
        ({"access_token": "s3cr3t-A"}, "x"),
        ({"refresh_token": "s3cr3t-A"}, "x"),
        ({"headers": {"X-Api-Key": "s3cr3t-A"}}, "x"),
        ({"session_id": "s3cr3t-A"}, "x"),
        ({"csrf": "s3cr3t-A"}, "x"),
        ({}, "retry access_token=s3cr3t-A now"),
        ({}, "Cookie: session=s3cr3t-A; csrftoken=s3cr3t-B"),
        ({}, "X-Api-Key: s3cr3t-A"),
        ({}, "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.s3cr3t-A"),
        ({}, '{"session": "s3cr3t-A"}'),
        ({}, "GET /verify?token=s3cr3t-A&x=1"),
    ],
)
def test_f06_credential_variants_redacted(buf, extra, msg):
    assert "s3cr3t" not in emit(buf, msg, extra=extra)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:hunter2@db:5432/x",
        "redis://:hunter2@redis:6379/0",
        "postgresql+psycopg://metis_app_login:hunter2@postgres/metis",
    ],
)
def test_f07_dsn_passwords_redacted(buf, dsn):
    assert "hunter2" not in emit(buf, "connecting to %s", dsn)
    assert "hunter2" not in emit(buf, "x", extra={"dsn": dsn})
    assert "hunter2" not in scrub_text(f"error: could not connect ({dsn}) refused")


def test_redaction_keeps_ordinary_text():
    s = "request finished status=200 path=/health duration_ms=12 user agent pool-size"
    assert scrub_text(s).startswith("request finished status=200 path=/health")


# ---- F-05 / F-10 ---------------------------------------------------------------------------------------------
def test_f05_production_error_does_not_echo_secrets():
    with pytest.raises(ValueError) as ei:
        Settings(
            env="production", redis_url="redis://:RedisPw77@r:6379/0", secret_key="Short-Secret-Key-Value"
        )
    assert "RedisPw77" not in str(ei.value) and "Short-Secret-Key-Value" not in str(ei.value)
    assert "METIS_SECRET_KEY" in str(ei.value)


def test_f05_get_settings_raises_clean_settings_error(monkeypatch):
    monkeypatch.setenv("METIS_ENV", "production")
    monkeypatch.setenv("METIS_REDIS_URL", "redis://:RedisPw77@r:6379/0")
    monkeypatch.setenv("METIS_SECRET_KEY", "Short-Secret-Key-Value")
    get_settings.cache_clear()
    try:
        with pytest.raises(SettingsError) as ei:
            get_settings()
    finally:
        get_settings.cache_clear()
    assert "RedisPw77" not in str(ei.value) and "Short-Secret-Key-Value" not in str(ei.value)


def test_f10_env_example_rejected_in_production():
    env = {}
    for line in (ROOT / "deploy/.env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.lower().removeprefix("metis_")] = v
    env["env"] = "production"
    kw = {k: v for k, v in env.items() if k in Settings.model_fields}
    with pytest.raises(ValueError) as ei:
        Settings(**kw)
    msg = str(ei.value)
    for name in ("DATABASE_URL", "SECRET_KEY", "REDIS_URL"):
        assert f"METIS_{name} looks like a placeholder" in msg


def test_f10_f11_f12_extra_settings_checks():
    good = dict(
        env="production",
        database_url="postgresql+psycopg://metis_app_login:Zk3pQ9vLw@db:5432/metis",
        secret_key="k" * 16 + "9Qz" * 6,
        redis_url="redis://:Zk3pQ9vLw@r:6379/0",
        object_store_endpoint="https://s3.eu-west-1.amazonaws.com",
        object_store_access_key="AKIAZ9ZQ",
        object_store_secret_key="zz9Q8wLk",
    )
    assert Settings(**good).env == "production"
    for over in (
        {"database_url": "postgresql://metis_owner:pw9Zq@db/metis"},
        {"database_url": "postgresql://%70ostgres:pw9Zq@db/metis"},
        {"database_url": "postgresql://db/metis?user=postgres&password=pw9Zq"},
        {"secret_key": "a" * 32},
    ):
        with pytest.raises(ValueError):
            Settings(**{**good, **over})


# ---- F-14 / F-15 / F-16 --------------------------------------------------------------------------------------
def fresh_db(pg, name):
    with psycopg.connect(pg, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}"')
        c.execute(f'CREATE DATABASE "{name}"')
    return make_conninfo(pg, dbname=name)


def test_f14_concurrent_first_run_migrations(pg, tmp_path):
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE t1(x int);")
    for trial in range(4):
        uri = fresh_db(pg, f"fw_race{trial}")
        res: list = []

        def run(uri=uri, res=res):
            try:
                res.append(apply_migrations(uri, tmp_path))
            except Exception as exc:  # noqa: BLE001
                res.append(exc)

        ts = [threading.Thread(target=run) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not [r for r in res if isinstance(r, Exception)], res
        assert sum(len(r) for r in res) == 1


def test_f15_checksum_recorded_and_tamper_detected(pg, tmp_path):
    uri = fresh_db(pg, "fw_tamper")
    f = tmp_path / "0001_a.sql"
    f.write_text("CREATE TABLE t1(x int);")
    assert apply_migrations(uri, tmp_path) == ["0001_a.sql"]
    with psycopg.connect(uri) as c:
        (cs,) = c.execute("SELECT checksum FROM schema_migrations").fetchone()
    assert re.fullmatch(r"[0-9a-f]{64}", cs)
    assert apply_migrations(uri, tmp_path) == []
    f.write_text("CREATE TABLE t1(x int); GRANT ALL ON t1 TO PUBLIC;")
    with pytest.raises(MigrationError, match="0001_a.sql"):
        apply_migrations(uri, tmp_path)


def test_f15_legacy_table_without_checksum_is_upgraded(pg, tmp_path):
    uri = fresh_db(pg, "fw_legacy")
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE t1(x int);")
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute(
            "CREATE TABLE schema_migrations (filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        c.execute("CREATE TABLE t1(x int)")
        c.execute("INSERT INTO schema_migrations (filename) VALUES ('0001_a.sql')")
    assert apply_migrations(uri, tmp_path) == []
    with psycopg.connect(uri) as c:
        assert c.execute("SELECT checksum FROM schema_migrations").fetchone()[0]
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE t1(x int); -- edited")
    with pytest.raises(MigrationError):
        apply_migrations(uri, tmp_path)


def test_f16_out_of_order_migration_rejected(pg, tmp_path):
    uri = fresh_db(pg, "fw_order")
    (tmp_path / "0002_b.sql").write_text("CREATE TABLE t2(x int);")
    apply_migrations(uri, tmp_path)
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE t1(x int);")
    with pytest.raises(MigrationError, match="0001_a.sql"):
        apply_migrations(uri, tmp_path)
