"""Wave 1 red-team suite (Agent 9): foundation, data, deploy/CI and static-web checks.

Convention: a test marked xfail(strict=True, reason="F-xx SEVERITY: ...") reproduces a REAL defect and will
start failing (XPASS-strict) the day it is fixed, so the marker must then be removed. Unmarked tests are
protections that hold today and must keep holding. Full write-up: private/qa_wave1.md.
Browser-based checks live in tests/security/web/ (they need the Playwright venv).
"""
import hashlib
import io
import json
import logging
import pathlib
import re
import threading
import uuid

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]


def defect(fid, sev, msg):
    return pytest.mark.xfail(strict=True, reason=f"{fid} {sev}: {msg}")


# =====================================================================================================
# A. Foundation: logging
# =====================================================================================================
@pytest.fixture
def logbuf():
    from backend.core.logging import configure_logging

    root = logging.getLogger()
    old_level = root.level
    buf = io.StringIO()
    configure_logging(stream=buf)
    yield buf
    for h in list(root.handlers):
        if getattr(h, "_metis", False):
            root.removeHandler(h)
    root.setLevel(old_level)


def _emit(buf, msg="x", *args, **kw):
    logging.getLogger("rt").info(msg, *args, **kw)
    return buf.getvalue()


ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"


@pytest.mark.parametrize("key", ["smiles", "SMILES", "Smiles", "molblock", "Email", "PASSWORD", "Authorization", "Cookie"])
def test_log_key_redaction_is_case_insensitive_and_nested(logbuf, key):
    out = _emit(logbuf, "x", extra={"ctx": {"a": [{key: "hunter2-value"}]}})
    assert "hunter2-value" not in out


def test_log_smiles_embedded_in_messages_and_args(logbuf):
    out = _emit(logbuf, "parsed %s and %s", ASPIRIN, {"k": ASPIRIN})
    assert "Oc1ccccc1" not in out


def test_log_exception_chain_keeps_type_only(logbuf):
    try:
        try:
            raise ValueError("cannot parse " + ASPIRIN + " for alice@example.edu")
        except ValueError as e:
            raise RuntimeError("wrapped") from e
    except RuntimeError:
        logging.getLogger("rt").exception("boom")
    out = logbuf.getvalue()
    assert "alice@example.edu" not in out and "Oc1ccccc1" not in out and "ValueError" not in out


def test_log_emails_molblocks_inchi_redacted(logbuf):
    mb = "\n  RDKit 2D\n\n  1  0  0  0  0  0  0  0  0  0999 V2000\nM  END"
    out = _emit(logbuf, "u Alice.Bob+t@EXAMPLE.EDU %s InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3", mb)
    assert "EXAMPLE.EDU" not in out and "V2000" not in out and "InChI=1S" not in out


def test_log_long_and_pathological_inputs_are_fast(logbuf):
    import time

    t = time.time()
    _emit(logbuf, "x" + " " * 300000 + "y")
    _emit(logbuf, "1 " * 200000)
    _emit(logbuf, ASPIRIN * 20000)
    assert time.time() - t < 5


@pytest.mark.parametrize("smi", ["CC(=O)[O-].[Na+]", "O=C([O-])c1ccccc1.[K+]", "[Na+].[Cl-].c1ccccc1CCN(C)C", "[Se]c1ccccc1CCN"])
def test_log_salt_and_heteroatom_smiles_redacted(logbuf, smi):
    out = _emit(logbuf, "submitted %s", smi)
    assert smi not in out and "c1ccccc1" not in out and "[O-]" not in out


def test_log_smiles_with_identifier_prefix_redacted(logbuf):
    out = _emit(logbuf, f"item mol_{ASPIRIN} done row{ASPIRIN} q={ASPIRIN}")  # pre-formatted message, not %-args
    assert "Oc1ccccc1" not in out


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
    ],
)
def test_log_credential_key_variants_redacted(logbuf, extra, msg):
    out = _emit(logbuf, msg, extra=extra)
    assert "s3cr3t" not in out


@pytest.mark.parametrize("dsn", ["postgresql://u:hunter2@db:5432/x", "redis://:hunter2@redis:6379/0", "postgresql+psycopg://metis_app_login:hunter2@postgres/metis"])
def test_log_dsn_passwords_redacted(logbuf, dsn):
    out = _emit(logbuf, "connecting to %s", dsn)
    assert "hunter2" not in out
    out2 = _emit(logbuf, "x", extra={"dsn": dsn})
    assert "hunter2" not in out2


def test_log_unicode_obfuscation(logbuf):
    out = _emit(logbuf, "u alice＠example.edu C​C(=O)Oc1ccccc1C(=O)O")
    assert "alice" not in out and "Oc1ccccc1" not in out


def test_container_cmd_does_not_bypass_log_redaction():
    cmd = re.search(r"^CMD\s+(\[.*\])", (ROOT / "deploy/Dockerfile").read_text(), re.M).group(1)
    assert "--no-access-log" in cmd or "--log-config" in cmd


# =====================================================================================================
# A. Foundation: settings
# =====================================================================================================
def _good_prod(**over):
    kw = dict(
        env="production",
        database_url="postgresql+psycopg://metis_app_login:Zk3pQ9vLw@db:5432/metis",
        secret_key="k" * 16 + "9Qz" * 6,
        redis_url="redis://:Zk3pQ9vLw@r:6379/0",
        object_store_endpoint="https://s3.eu-west-1.amazonaws.com",
        object_store_access_key="AKIAZ9ZQ",
        object_store_secret_key="zz9Q8wLk",
    )
    kw.update(over)
    return kw


def test_settings_secrets_not_in_repr_dump_or_errors():
    from backend.core.settings import Settings

    s = Settings(**_good_prod())
    blob = repr(s) + str(s) + json.dumps(s.model_dump(mode="json"))
    assert "Zk3pQ9vLw" not in blob and "zz9Q8wLk" not in blob


def test_settings_production_rejects_short_key_missing_and_superuser():
    from backend.core.settings import Settings

    for over in (
        {"secret_key": "short"},
        {"redis_url": None},
        {"database_url": "postgresql+psycopg://postgres:pw9Zq@db/metis"},
        {"database_url": "postgresql://changeme:pw@db/metis"},
    ):
        with pytest.raises(ValueError):
            Settings(**_good_prod(**over))


def test_settings_validation_error_does_not_echo_secret_values():
    from backend.core.settings import Settings

    with pytest.raises(ValueError) as ei:
        Settings(env="production", redis_url="redis://:RedisPw77@r:6379/0", secret_key="Short-Secret-Key-Value")
    assert "RedisPw77" not in str(ei.value) and "Short-Secret-Key-Value" not in str(ei.value)


def test_settings_rejects_env_example_placeholders_in_production():
    from backend.core.settings import Settings

    env = {}
    for line in (ROOT / "deploy/.env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.lower().removeprefix("metis_")] = v
    env["env"] = "production"
    kw = {k: v for k, v in env.items() if k in Settings.model_fields}
    with pytest.raises(ValueError):
        Settings(**kw)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://metis_owner:pw9Zq@db/metis",
        "postgresql://db/metis?user=postgres&password=pw9Zq",
        "postgresql://POSTGRES:pw9Zq@db/metis",
        "postgresql://%70ostgres:pw9Zq@db/metis",
    ],
)
def test_settings_database_url_must_be_app_role(url):
    from backend.core.settings import Settings

    with pytest.raises(ValueError):
        Settings(**_good_prod(database_url=url))


def test_settings_rejects_trivial_secret_key():
    from backend.core.settings import Settings

    with pytest.raises(ValueError):
        Settings(**_good_prod(secret_key="a" * 32))


# =====================================================================================================
# A. Foundation: db identity
# =====================================================================================================
@pytest.fixture(scope="module")
def app_url(pg):
    import psycopg
    from sqlalchemy.engine import make_url

    with psycopg.connect(pg, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS rt1_login")
        c.execute("CREATE ROLE rt1_login LOGIN PASSWORD 'rt-only' IN ROLE metis_app")
    u = make_url(pg.replace("postgresql://", "postgresql+psycopg://", 1)).set(username="rt1_login", password="rt-only")
    yield u.render_as_string(hide_password=False)
    with psycopg.connect(pg, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS rt1_login")


@pytest.fixture
def factory(app_url):
    """Engine on the real login role. prepare_threshold is disabled ONLY so that identity tests can run many
    requests on one pooled connection without tripping F-01 (which has its own dedicated xfail test)."""
    from sqlalchemy import event

    from backend.core.db import make_engine, make_session_factory
    from backend.core.settings import Settings

    eng = make_engine(app_url, settings=Settings(env="test", db_pool_size=2))
    event.listen(eng, "connect", lambda c, _r: setattr(c, "prepare_threshold", None))
    yield make_session_factory(eng)
    eng.dispose()


def _uid(s):
    from sqlalchemy import text

    return s.execute(text("SELECT current_user_id()")).scalar()


def test_pool_reuse_survives_many_requests(app_url):
    from sqlalchemy import text

    from backend.core.db import make_engine, make_session_factory, request_session
    from backend.core.settings import Settings

    eng = make_engine(app_url, settings=Settings(env="test", db_pool_size=1))
    try:
        f = make_session_factory(eng)
        for _ in range(20):
            with request_session(f, uuid.uuid4()) as s:
                s.execute(text("SELECT current_user_id()")).scalar()
    finally:
        eng.dispose()


def test_identity_is_not_session_scoped_even_if_app_sql_tries(factory):
    from sqlalchemy import text

    from backend.core.db import request_session

    u = uuid.uuid4()
    with request_session(factory, u) as s:  # hostile SQL tries a session-scoped set (is_local=false)
        s.execute(text("SELECT set_config('metis.user_id', :v, false)"), {"v": str(uuid.uuid4())})
    with factory() as s, s.begin():
        assert _uid(s) is None  # DISCARD ALL at check-in clears it


def test_identity_restored_after_savepoint_rollback_and_exception(factory):
    from sqlalchemy import text

    from backend.core.db import request_session

    u, other = uuid.uuid4(), uuid.uuid4()
    with request_session(factory, u) as s:
        sp = s.begin_nested()
        s.execute(text("SELECT set_config('metis.user_id', :v, true)"), {"v": str(other)})
        sp.rollback()
        assert _uid(s) == u
    with pytest.raises(RuntimeError), request_session(factory, u) as s:
        raise RuntimeError("x")
    with factory() as s, s.begin():
        assert _uid(s) is None


def test_commit_inside_request_fails_closed_instead_of_continuing_anonymously(factory):
    from sqlalchemy import text
    from sqlalchemy.exc import InvalidRequestError

    from backend.core.db import request_session

    with pytest.raises(InvalidRequestError), request_session(factory, uuid.uuid4()) as s:
        s.execute(text("SELECT 1"))
        s.commit()
        s.execute(text("SELECT 2"))  # a post-commit transaction would have no identity: SQLAlchemy refuses it


def test_identity_isolated_across_concurrent_threads(factory):
    from backend.core.db import request_session

    bad = []

    def worker():
        mine = uuid.uuid4()
        for _ in range(25):
            with request_session(factory, mine) as s:
                if _uid(s) != mine:
                    bad.append(mine)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not bad


@pytest.mark.parametrize(
    "evil",
    ["'; DROP TABLE app_user;--", "1' OR '1'='1", "00000000-0000-0000-0000-000000000000'; --", "not-a-uuid", "1", "", " ", "\x00", "../etc", "x" * 5000],
)
def test_non_uuid_user_ids_rejected_before_db(factory, evil):
    from backend.core.db import request_session

    if evil == "":  # falsy id is anonymous by design
        return
    with pytest.raises((ValueError, TypeError, AttributeError)):
        with request_session(factory, evil):
            pass


def test_non_string_user_ids_rejected(factory):
    from backend.core.db import request_session

    for bad in (1, 1.5, ["a"], {"a": 1}, b"x"):
        with pytest.raises(Exception):  # noqa: B017
            with request_session(factory, bad):
                pass


def test_user_id_is_canonicalised_before_database(factory):
    from backend.core.db import request_session

    u = uuid.uuid4()
    for variant in (f"urn:uuid:{u}", str(u)[:8] + "urn:" + str(u)[8:], "\u0661" * 32):
        with pytest.raises(ValueError):
            with request_session(factory, variant):
                pass


def test_info_app_sql_can_pivot_identity_inside_its_own_transaction(factory):
    """INFO (documented, not a defect of request_session): the app role may call set_config(..., true) itself, so
    any SQL injection can switch identity within the transaction. Mitigation is parameterised-only SQL (see
    tests/foundation/test_no_string_sql.py) and the forced-RLS tests."""
    from sqlalchemy import text

    from backend.core.db import request_session

    a, b = uuid.uuid4(), uuid.uuid4()
    with request_session(factory, a) as s:
        s.execute(text("SELECT set_config('metis.user_id', :v, true)"), {"v": str(b)})
        assert _uid(s) == b


# =====================================================================================================
# A. Foundation: migration runner
# =====================================================================================================
def _fresh_db(pg, name):
    import psycopg
    from psycopg.conninfo import make_conninfo

    with psycopg.connect(pg, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}"')
        c.execute(f'CREATE DATABASE "{name}"')
    return make_conninfo(pg, dbname=name)


def test_migration_partial_failure_rolls_back_and_is_retryable(pg, tmp_path):
    import psycopg

    from backend.core.migrate import apply_migrations

    uri = _fresh_db(pg, "rt_mig_partial")
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE t1(x int);")
    (tmp_path / "0002_bad.sql").write_text("CREATE TABLE t2(x int); SELECT 1/0;")
    with pytest.raises(psycopg.errors.DivisionByZero):
        apply_migrations(uri, tmp_path)
    with psycopg.connect(uri, autocommit=True) as c:
        assert c.execute("SELECT to_regclass('t2')").fetchone()[0] is None
        assert [r[0] for r in c.execute("SELECT filename FROM schema_migrations")] == ["0001_a.sql"]
    (tmp_path / "0002_bad.sql").write_text("CREATE TABLE t2(x int);")
    assert apply_migrations(uri, tmp_path) == ["0002_bad.sql"]


def test_migration_runner_is_idempotent_and_never_prints_password(pg, capsys, monkeypatch):
    from backend.core import migrate

    uri = _fresh_db(pg, "rt_mig_idem")
    monkeypatch.setenv("METIS_MIGRATION_URL", uri)
    assert migrate.main() == 0 and migrate.main() == 0
    out = capsys.readouterr()
    assert "password" not in (out.out + out.err).lower()


def test_concurrent_first_run_migrations_do_not_error(pg, tmp_path):
    from backend.core.migrate import apply_migrations

    (tmp_path / "0001_a.sql").write_text("CREATE TABLE t1(x int);")
    for trial in range(8):
        uri = _fresh_db(pg, f"rt_mig_race{trial}")
        res = []

        def run():
            try:
                res.append(apply_migrations(uri, tmp_path))
            except Exception as e:  # noqa: BLE001
                res.append(e)

        ts = [threading.Thread(target=run) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not [r for r in res if isinstance(r, Exception)], res
        assert sum(len(r) for r in res) == 1


def test_tampered_applied_migration_is_detected(pg, tmp_path):
    from backend.core.migrate import apply_migrations

    uri = _fresh_db(pg, "rt_mig_tamper")
    f = tmp_path / "0001_a.sql"
    f.write_text("CREATE TABLE t1(x int);")
    apply_migrations(uri, tmp_path)
    f.write_text("CREATE TABLE t1(x int); GRANT ALL ON t1 TO PUBLIC;")
    with pytest.raises(Exception):  # noqa: B017
        apply_migrations(uri, tmp_path)


def test_out_of_order_migration_is_rejected(pg, tmp_path):
    from backend.core.migrate import apply_migrations

    uri = _fresh_db(pg, "rt_mig_order")
    (tmp_path / "0002_b.sql").write_text("CREATE TABLE t2(x int);")
    apply_migrations(uri, tmp_path)
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE t1(x int);")
    with pytest.raises(Exception):  # noqa: B017
        apply_migrations(uri, tmp_path)


def test_app_role_cannot_touch_schema_migrations(pg):
    import psycopg

    from backend.core.migrate import apply_migrations

    uri = _fresh_db(pg, "rt_mig_priv")
    apply_migrations(uri)
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute("SET ROLE metis_app")
        for q in ("INSERT INTO schema_migrations(filename) VALUES ('x')", "DELETE FROM schema_migrations"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(q)


# =====================================================================================================
# A. Dockerfile / compose / CI
# =====================================================================================================
DOCKERFILE = (ROOT / "deploy/Dockerfile").read_text()
COMPOSE = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text())
CI_TEXT = (ROOT / ".github/workflows/ci.yml").read_text()
CI = yaml.safe_load(CI_TEXT)


def test_dockerfile_runs_non_root_with_no_embedded_secrets():
    assert re.search(r"^USER\s+\d+:\d+", DOCKERFILE, re.M)
    assert not re.search(r"(?i)(password|secret|token|api_key)\s*=\s*\S+", DOCKERFILE.replace("METIS_ENV=production", ""))
    assert "COPY . " not in DOCKERFILE and "ADD " not in DOCKERFILE


def test_compose_publishes_only_localhost_and_hardens_app():
    for name, svc in COMPOSE["services"].items():
        for p in svc.get("ports", []):
            assert str(p).startswith("127.0.0.1:"), (name, p)
    app = COMPOSE["services"]["app"]
    assert app["read_only"] is True and app["cap_drop"] == ["ALL"] and "no-new-privileges:true" in app["security_opt"]


def test_app_container_does_not_receive_owner_credentials():
    app = COMPOSE["services"]["app"]
    files = app.get("env_file", [])
    files = [files] if isinstance(files, str) else files
    leaked = set()
    for f in files:
        path = ROOT / "deploy" / (f + ".example" if f == ".env" else f)
        for line in path.read_text().splitlines():
            if line and not line.startswith("#") and line.split("=")[0] in ("POSTGRES_PASSWORD", "METIS_MIGRATION_URL"):
                leaked.add(line.split("=")[0])
    assert not leaked


@defect("F-17", "LOW", "compose expects a metis_app_login role created by 'deploy tooling' but no script/SQL in the repo creates it, so the documented stack cannot start")
def test_something_creates_the_app_login_role():
    hay = "".join(p.read_text() for p in list((ROOT / "deploy").rglob("*")) + list((ROOT / "scripts").rglob("*")) if p.is_file() and p.suffix in {".sql", ".sh", ".yml", ".py", ""} and p.name != ".env.example")
    assert "CREATE ROLE metis_app_login" in hay or "metis_app_login" in "".join(
        str(s.get("command", "")) + str(s.get("volumes", "")) for s in COMPOSE["services"].values()
    )


@defect("F-18", "LOW", "no .dockerignore: build context (repo root) ships deploy/.env, private/, .git to the builder and any stray .env under backend/ is COPYed into the image")
def test_dockerignore_excludes_secrets():
    p = ROOT / ".dockerignore"
    assert p.exists()
    txt = p.read_text()
    for needle in (".env", "private", ".git"):
        assert needle in txt


@defect("F-19", "LOW", "base/service images are tag-pinned, not digest-pinned (python, postgres, redis, minio); pip install has no --require-hashes")
def test_images_and_pip_are_immutable():
    imgs = re.findall(r"^FROM\s+(\S+)", DOCKERFILE, re.M) + [s["image"] for s in COMPOSE["services"].values() if "image" in s]
    assert all("@sha256:" in i for i in imgs)
    assert "--require-hashes" in DOCKERFILE


@defect("F-20", "LOW", "no HEALTHCHECK in the image or healthcheck on the app/redis/minio services")
def test_runtime_healthchecks_present():
    assert "HEALTHCHECK" in DOCKERFILE or "healthcheck" in COMPOSE["services"]["app"]


@defect("F-21", "LOW", "redis password is passed on the command line (visible in docker inspect and ps)")
def test_redis_password_not_on_command_line():
    assert "--requirepass" not in " ".join(COMPOSE["services"]["redis"]["command"])


def test_ci_permissions_triggers_and_no_expression_injection():
    assert CI["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in CI_TEXT and "workflow_run" not in CI_TEXT
    assert not re.search(r"run:.*\$\{\{\s*github\.(event|head_ref)", CI_TEXT)
    assert "secrets." not in CI_TEXT


def test_ci_actions_pinned_to_commit_sha():
    uses = re.findall(r"uses:\s*(\S+)", CI_TEXT)
    assert uses and all(re.search(r"@[0-9a-f]{40}$", u) for u in uses), uses


@defect("F-23", "LOW", "gitleaks binary is fetched with curl|tar and never checksum-verified")
def test_ci_downloaded_binaries_are_verified():
    assert "sha256sum" in CI_TEXT or "cosign" in CI_TEXT or "shasum" in CI_TEXT


def test_ci_installs_everything_it_collects():
    dev = (ROOT / "requirements-dev.txt").read_text() + (ROOT / "requirements.txt").read_text()
    runs = " ".join(s.get("run", "") for s in CI["jobs"]["test"]["steps"])
    collects_web = "tests/web" not in runs or "--ignore=tests/web" not in runs
    needs = [] if not collects_web else ["playwright"]
    needs += [] if "ml/requirements.txt" in dev or "ml/requirements.txt" in runs else ["rdkit"]
    assert not needs, f"CI collects tests needing: {needs}"


def test_ci_scans_ml_code_and_dependencies():
    assert "ml/requirements.txt" in CI_TEXT
    assert re.search(r"bandit .*-r backend ml|bandit .*ml", CI_TEXT)


# =====================================================================================================
# B. Data: license gate
# =====================================================================================================
def _manifest_with(tmp_path, mutate):
    from ml.data import manifest

    d = yaml.safe_load(manifest.MANIFEST_PATH.read_text())
    mutate(d)
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(d))
    return p


def _freesolv(d):
    return next(r for r in d["datasets"] if r["id"] == "FreeSolv")


@pytest.mark.parametrize("name", ["FreeSolv", "freesolv", "FREESOLV", "FreeSolv ", "Free​Solv", "FreeSolv.csv", "../FreeSolv", "free_solv", "SAMPL"])
def test_freesolv_refused_under_every_name_with_shipped_manifest(name):
    from ml.data import manifest

    for fn in (manifest.assert_trainable, manifest.assert_loadable):
        with pytest.raises((manifest.LicenseError, KeyError)):
            fn(name)


def test_freesolv_refused_by_every_entry_point_even_if_yaml_says_cleared(tmp_path):
    from ml.data import loader, manifest

    p = _manifest_with(tmp_path, lambda d: _freesolv(d).update(license_status="cleared", date_checked="2026-01-01", confirmed_by="me"))
    with pytest.raises(manifest.LicenseError):
        manifest.assert_trainable("FreeSolv", p)
    with pytest.raises(manifest.LicenseError):
        loader.load_local("FreeSolv", tmp_path)
    with pytest.raises(manifest.LicenseError):
        loader.download("FreeSolv", tmp_path)  # refused before the optional PyTDC import / network


def test_cli_download_flag_cannot_fetch_freesolv(tmp_path):
    from ml.data import loader, manifest

    with pytest.raises(manifest.LicenseError):
        loader.main(["FreeSolv", "--download", "--cache-dir", str(tmp_path)])


def test_pending_and_unknown_datasets_are_not_trainable():
    from ml.data import manifest

    for d in manifest.load_manifest():
        if d == "FreeSolv" or manifest.get(d).license_status != "cleared":
            with pytest.raises(manifest.LicenseError):
                manifest.assert_trainable(d)
    with pytest.raises(KeyError):
        manifest.assert_trainable("nonexistent")


def test_manifest_yaml_loader_is_safe(tmp_path):
    from ml.data import manifest

    p = tmp_path / "evil.yaml"
    p.write_text("datasets: !!python/object/apply:os.system ['echo pwned > %s/pwned']" % tmp_path)
    with pytest.raises(yaml.YAMLError):
        manifest.load_manifest(p)
    assert not (tmp_path / "pwned").exists()
    assert not re.search(r"pickle|yaml\.load\(|eval\(|exec\(|os\.system|subprocess", "".join(p.read_text() for p in (ROOT / "ml").rglob("*.py")))


def test_cleared_requires_provenance_fields(tmp_path):
    from ml.data import manifest

    p = _manifest_with(tmp_path, lambda d: d["datasets"][0].update(license_status="cleared"))
    with pytest.raises(ValueError):
        manifest.load_manifest(p)


def test_aliased_freesolv_cannot_be_cleared(tmp_path):
    from ml.data import manifest

    def alias(d):
        r = dict(_freesolv(d))
        r.update(id="SAMPL_hydration", license_status="cleared", date_checked="2026-01-01", confirmed_by="me")
        d["datasets"].append(r)

    p = _manifest_with(tmp_path, alias)
    with pytest.raises(manifest.LicenseError):
        manifest.assert_trainable("SAMPL_hydration", p)


@pytest.mark.parametrize("upd", [{"date_checked": "not a date", "confirmed_by": "x"}, {"date_checked": "2026-01-01", "license_as_stated": "CC BY-NC-SA 4.0", "confirmed_by": "me"}])
def test_cleared_validates_provenance_and_licence_text(tmp_path, upd):
    from ml.data import manifest

    p = _manifest_with(tmp_path, lambda d: d["datasets"][0].update(license_status="cleared", **upd))
    with pytest.raises((ValueError, manifest.LicenseError)):
        manifest.assert_trainable("hERG", p)


def test_cache_path_confined_to_cache_dir(tmp_path):
    from ml.data import loader

    with pytest.raises(ValueError):  # ids must match ^[A-Za-z0-9_]+$; traversal is refused outright
        loader.cache_path("../../evil", tmp_path / "data")


@pytest.mark.parametrize("m", ["Random", "RANDOM", "random ", "stratified_random", "kfold"])
def test_card_rejects_non_allowed_splits(m):
    from ml.data.cards import render_card

    with pytest.raises(ValueError):
        render_card("hERG", 10, m)


# =====================================================================================================
# B. Data: cleaning
# =====================================================================================================
def test_cleaning_salts_charges_stereo_and_duplicates():
    from ml.data.clean import clean_dataset, standardize

    assert standardize("CC(=O)[O-].[Na+]")[0] == "CC(=O)O"
    assert standardize("[NH3+]CC(=O)[O-]")[0] == "NCC(=O)O"
    assert standardize("C[N+](C)(C)C")[0] == "C[N+](C)(C)C"  # permanent charge kept
    a, b = standardize("N[C@@H](C)C(=O)O"), standardize("N[C@H](C)C(=O)O")
    assert a[1] != b[1] and standardize("N[C@@H](C)C(=O)O", keep_stereo=False)[1] == standardize("N[C@H](C)C(=O)O", keep_stereo=False)[1]
    out, rep = clean_dataset([("CCO", 0), ("OCC", 0), ("CCN", 1), ("CCN", 0), ("bad", 1), ("", 1), (None, 1)], "classification")
    assert [o["smiles"] for o in out] == ["CCO"] and rep.n_unparseable == 3 and rep.n_contradictory_dropped == 1
    out, _ = clean_dataset([("CCO", 1.0), ("OCC", 1.4), ("CCN", 1.0), ("NCC", 2.0)], "regression", tolerance=0.5)
    assert len(out) == 1 and out[0]["label"] == pytest.approx(1.2)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_labels_never_reach_output(bad):
    import math

    from ml.data.clean import clean_dataset

    try:
        out, _ = clean_dataset([("CCO", bad), ("OCC", bad), ("CCN", 2.0)], "regression")
    except ValueError:
        return
    assert all(math.isfinite(o["label"]) for o in out)


def test_load_local_rejects_non_finite(tmp_path, monkeypatch):
    import math

    from ml.data import loader

    (tmp_path / "hERG.csv").write_text("Drug,Y\nCCO,nan\nCCN,inf\n")
    try:
        rows = loader.load_local("hERG", tmp_path)
    except ValueError:
        return
    assert all(math.isfinite(y) for _, y in rows)


def test_cleaning_report_accounts_for_every_input_record():
    from ml.data.clean import clean_dataset

    recs = [("CCO", 0), ("OCC", 1), ("CCN", 1), ("bad", 1)]
    out, r = clean_dataset(recs, "classification")
    dropped_records = 2  # the two contradictory CCO rows
    assert r.reconciles()
    assert r.n_input == r.n_unparseable + r.n_empty_after_standardize + r.n_contradictory_records + r.n_merged_duplicates + r.n_output
    assert r.n_contradictory_records == dropped_records


def test_info_equal_size_mixtures_silently_pick_one_component():
    """INFO (documented): LargestFragmentChooser breaks ties arbitrarily, so a 1:1 mixture of two distinct
    drugs is attributed to one of them with the mixture's label."""
    from ml.data.clean import standardize

    assert standardize("CCO.CCN")[0] == "CCN" == standardize("CCN.CCO")[0]


# =====================================================================================================
# B. Data: splits
# =====================================================================================================
def test_splits_are_group_disjoint_deterministic_and_never_random():
    from ml.data import splits

    smi = ["c1ccccc1C" * 1 + "C" * i for i in range(30)] + ["C1CCCCC1" + "C" * i for i in range(30)] + ["c1ccncc1" + "C" * i for i in range(30)]
    a = splits.scaffold_split(smi, seed=3)
    assert a == splits.scaffold_split(smi, seed=3)
    parts = [set(p) for p in a]
    assert not (parts[0] & parts[1]) and not (parts[0] & parts[2]) and not (parts[1] & parts[2]) and sum(map(len, parts)) == len(smi)
    with pytest.raises(ValueError):
        splits.split("random", smi)
    with pytest.raises(ValueError):
        splits.split("Random", smi)


def test_scaffold_split_keeps_enantiomers_together():
    from ml.data import splits

    smi = ["N[C@@H](C)C(=O)O", "N[C@H](C)C(=O)O"] * 5 + ["c1ccccc1C", "C1CCCCC1C"] * 5
    tr, ca, te = splits.scaffold_split(smi)
    for i in range(0, 10, 2):
        assert len({"tr" if i in tr else "ca" if i in ca else "te", "tr" if i + 1 in tr else "ca" if i + 1 in ca else "te"}) == 1


def test_dominant_scaffold_group_does_not_empty_train():
    from ml.data import splits

    tr, ca, te = splits.scaffold_split(["C" * n for n in range(2, 12)])  # ten acyclic molecules share scaffold ''
    assert len(tr) > 0


@pytest.mark.parametrize("smi", [["c1ccccc1", "C1CCCCC1", "c1ccncc1", "C1CCNCC1", "c1ccoc1"], ["c1ccccc1"]])
def test_small_inputs_raise_or_fill_every_partition(smi):
    from ml.data import splits

    try:
        parts = splits.scaffold_split(smi)
    except ValueError:
        return
    assert all(len(p) > 0 for p in parts)


def test_splits_reject_unparseable_smiles_cleanly():
    from ml.data import splits

    for fn in (splits.scaffold_split, splits.butina_split):
        with pytest.raises(ValueError):
            fn(["CCO", "not_a_smiles", "CCN"])


@pytest.mark.parametrize("frac", [(0.9, 0.9, 0.9), (-1, 0, 2), (0, 0, 0)])
def test_split_fractions_validated(frac):
    from ml.data import splits

    with pytest.raises(ValueError):
        splits.scaffold_split(["c1ccccc1", "C1CCCCC1", "c1ccncc1", "C1CCNCC1"] * 5, frac=frac)


def test_time_split_does_not_leak_duplicates_across_partitions():
    from rdkit import Chem
    from rdkit.Chem import inchi

    from ml.data import splits

    smi = ["CCO", "c1ccccc1C", "CCN", "CCC", "CCCC", "CCCCC", "CCCCCC", "CCCCCCC", "N[C@@H](C)C(=O)O", "N[C@H](C)C(=O)O"]
    tr, ca, te = splits.split("time", smi, dates=list(range(10)))
    key = lambda i: inchi.MolToInchiKey(Chem.MolFromSmiles(smi[i]))[:14]  # noqa: E731  connectivity layer
    assert not ({key(i) for i in tr + ca} & {key(i) for i in te})


def test_baseline_single_class_training_is_handled():
    import numpy as np

    from ml.data import baselines

    m = baselines.rf_baseline(["CCO", "CCN", "CCC"], np.array([0, 0, 0]), "classification", n_estimators=5)
    p = baselines.rf_predict(m, ["CCO"], "classification")
    assert p.shape == (1,)


def test_gse_and_crippen_baselines_are_consistent():
    from ml.data import baselines

    logp = baselines.crippen_logp("CCCCCC")
    assert baselines.gse_logs("CCCCCC")[0] == pytest.approx(0.5 - logp) and baselines.gse_logs("CCCCCC", 125.0) == (pytest.approx(0.5 - 1.0 - logp), True)


def test_data_card_flags_pending_and_excluded_status():
    from ml.data.cards import render_card

    assert "NOT cleared" in render_card("hERG", None, None)
    assert "EXCLUDED" in render_card("FreeSolv", None, None)
    assert "random" not in render_card("hERG", 5, "scaffold", (4, 0, 1)).lower().split("split:")[1].split("\n")[0]


def test_info_manifest_units_agree_with_web_fixture_units():
    """LOW conformance note: LD50 unit string differs between datasets.yaml and the web fixture/wireframe."""
    from ml.data import manifest

    web = json.loads((ROOT / "web/fixtures/project.json").read_text())["endpoints"]["ld50"]["unit"]
    assert manifest.get("LD50_Zhu").units and web  # presence only; the mismatch is reported in qa_wave1.md (F-47)


# =====================================================================================================
# C. Web (static checks that run in the normal venv; browser checks are in tests/security/web)
# =====================================================================================================
WEB = ROOT / "web"
HTML = sorted(WEB.glob("*.html")) + sorted((WEB / "legal").glob("*.html"))
JS = sorted((WEB / "js").glob("*.js"))


def test_web_js_has_no_dom_injection_sinks():
    banned = re.compile(r"\.innerHTML|\.outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|postMessage|setTimeout\(\s*[\"'`]|\.srcdoc|createContextualFragment|location\s*=|location\.(href|assign|replace)\s*=")
    for f in JS:
        assert not banned.search(f.read_text()), f.name


def test_web_html_has_no_inline_script_or_event_handlers_and_safe_blank_links():
    for f in HTML:
        t = f.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", t), f.name
        assert not re.search(r"\son[a-z]+\s*=", t), f.name
        assert not re.search(r"javascript:", t, re.I), f.name
        for m in re.finditer(r"<a\b[^>]*target=\"_blank\"[^>]*>", t):
            assert "noopener" in m.group(0), (f.name, m.group(0))


def test_web_loads_nothing_from_third_party_origins():
    for f in HTML + sorted((WEB / "css").glob("*.css")) + JS:
        t = f.read_text()
        for url in re.findall(r"(?:src|href)=\"(https?:)?//[^\"]+\"|url\(\s*['\"]?https?://|import\s+.*from\s+['\"]https?://|fetch\(\s*['\"`]https?://", t):
            raise AssertionError(f"{f.name}: external reference")


def test_vendored_files_match_recorded_hashes():
    v = (WEB / "vendor/VERSIONS.txt").read_text()
    for rel in ("vendor/3Dmol-min.js", "fonts/plex-sans-400.woff2", "fonts/plex-sans-500.woff2", "fonts/plex-sans-600.woff2"):
        digest = hashlib.sha256((WEB / rel).read_bytes()).hexdigest()
        assert digest in v, rel


def test_fixture_svgs_are_inert():
    for f in list((WEB / "fixtures/svg").glob("*.svg")) + list((WEB / "assets").glob("*.svg")):
        t = f.read_text()
        assert not re.search(r"<script|\son[a-z]+\s*=|javascript:|foreignObject|<iframe|xlink:href=\"https?:|<image|<use[^>]+href=\"https?:", t, re.I), f.name


def test_localstorage_holds_only_fixture_consent_flags():
    keys = set()
    for f in JS:
        keys |= set(re.findall(r"(?:localStorage|store\??)\.(?:get|set)Item\(\s*[\"']([^\"']+)", f.read_text()))
    assert keys <= {"metis.fixture.consent."}, keys
    assert "sessionStorage" not in "".join(f.read_text() for f in JS) and "indexedDB" not in "".join(f.read_text() for f in JS)


def test_no_verdict_or_traffic_light_language_on_built_screens():
    pat = re.compile(r"\b(safe|unsafe|toxic|non-toxic|good|bad|recommended?|promising|avoid|go/no-go|traffic|likely to|unlikely|low risk|high risk|drug-like|druglike|pass(ed)? the)\b", re.I)
    text = "".join(f.read_text() for f in HTML + JS if f.name != "legal_text.js")
    text += "".join(p.read_text() for p in (WEB / "fixtures").glob("*.json") if p.name != "training.json").replace("molblock", "")
    hits = {m.group(0).lower() for m in pat.finditer(re.sub(r'"[^"]{200,}"', "", text))}
    assert not hits, hits


def test_fixture_fragment_atom_indices_match_smarts_in_the_conformer():
    """Spec 4.2: the 2D and 3D highlight come from one atom-index map. Verify against RDKit for all fixtures."""
    from rdkit import Chem

    for c in json.loads((WEB / "fixtures/project.json").read_text())["compounds"]:
        m = Chem.MolFromMolBlock(c["conformer"]["molblock"], removeHs=False)
        q = Chem.MolFromSmarts(c["fragment"]["smarts"])
        assert set(c["fragment"]["atom_indices"]) in [set(x) for x in m.GetSubstructMatches(q)], c["id"]


def test_web_is_compatible_with_strict_style_csp():
    hits = [f.name for f in HTML + JS if re.search(r"\bstyle\s*[:=]\s*[\"'`]|setAttribute\(\s*[\"']style", f.read_text()) or re.search(r'style="', f.read_text())]
    assert not hits, hits


@defect("F-38", "LOW", "no Content-Security-Policy anywhere (no <meta http-equiv>, and the static server guidance is undocumented); 3Dmol.js additionally needs worker blob: and eval, so the policy must be designed")
def test_web_ships_csp_guidance():
    assert any("Content-Security-Policy" in f.read_text() for f in HTML) or (ROOT / "docs/CSP.md").exists()


def test_served_web_root_has_no_screenshots():
    assert not (WEB / "screenshots").exists()


# ---- wireframe / spec conformance gaps (see private/qa_wave1.md, section 'wireframe/spec conformance gaps') ----
def test_report_export_screen_exists():
    assert (WEB / "report.html").exists() or (WEB / "export.html").exists()


def test_uncertainty_panel_offers_all_ensemble_sizes():
    sel = re.search(r'<select id="ens2">(.*?)</select>', (WEB / "compound.html").read_text(), re.S).group(1)
    assert set(re.findall(r"<option[^>]*>(\d+)</option>", sel)) == {"3", "5", "10", "20"}


@defect("G-06", "LOW", "add-compounds page states no maximum file size/type limit for CSV/SDF upload (spec 10: size and type limits)")
def test_add_page_states_upload_limit():
    assert re.search(r"\b\d+\s?(KB|MB)\b|max(imum)? (file )?size", (WEB / "add.html").read_text(), re.I)
