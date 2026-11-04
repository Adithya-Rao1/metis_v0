"""Fixtures for the red-team suite (independent of tests/db/conftest.py so it can run alone)."""
import pathlib
import pytest
import pgserver
import psycopg

MIGRATION = pathlib.Path(__file__).parents[2] / "backend/db/migrations/0001_baseline.sql"


@pytest.fixture(scope="session")
def pg(tmp_path_factory):
    srv = pgserver.get_server(tmp_path_factory.mktemp("pg_rt"), cleanup_mode="stop")
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute(MIGRATION.read_text())
    yield uri
    srv.cleanup()


@pytest.fixture
def su(pg):
    with psycopg.connect(pg, autocommit=True) as c:
        yield c
        c.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_no_update")
        c.execute("TRUNCATE app_user, model_version, deletion_request, audit_log CASCADE")
        c.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_no_update")


@pytest.fixture
def appc(pg, su):
    """Factory: autocommit connection acting as metis_app with a session-level user id."""
    conns = []

    def make(uid=None):
        c = psycopg.connect(pg, autocommit=True)
        c.execute("SET ROLE metis_app")
        c.execute("SELECT set_config('metis.user_id', %s, false)", (str(uid) if uid else "",))
        conns.append(c)
        return c

    yield make
    for c in conns:
        try:
            c.close()
        except Exception:
            pass


@pytest.fixture
def login_role(pg, su):
    """A real LOGIN role that is a plain member of metis_app (how deploy should connect the API)."""
    su.execute("DROP ROLE IF EXISTS rt_login")
    su.execute("CREATE ROLE rt_login LOGIN IN ROLE metis_app")
    conns = []

    def make(uid=None):
        c = psycopg.connect(pg, user="rt_login", autocommit=True)
        c.execute("SELECT set_config('metis.user_id', %s, false)", (str(uid) if uid else "",))
        conns.append(c)
        return c

    yield make
    for c in conns:
        c.close()
    su.execute("DROP ROLE IF EXISTS rt_login")
