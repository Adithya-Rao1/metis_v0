import pathlib
import pytest
import pgserver
import psycopg

MIGRATIONS = sorted((pathlib.Path(__file__).parents[2] / "backend/db/migrations").glob("*.sql"))


@pytest.fixture(scope="session")
def pg(tmp_path_factory):
    srv = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        for m in MIGRATIONS:
            c.execute(m.read_text())
    yield uri
    srv.cleanup()


@pytest.fixture
def su(pg):
    """Superuser connection (migration role stand-in); cleans tables after each test."""
    with psycopg.connect(pg, autocommit=True) as c:
        yield c
        c.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_no_update")
        c.execute("TRUNCATE app_user, model_version, deletion_request, audit_log CASCADE")
        c.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_no_update")


class App:
    """Connection acting as the least-privileged app role, with per-request user id."""
    def __init__(self, uri):
        self.conn = psycopg.connect(uri, autocommit=False)
        self.conn.execute("SET ROLE metis_app")
        self.conn.commit()

    def as_user(self, uid):
        self.conn.rollback()
        self.conn.execute("SET ROLE metis_app")
        self.conn.execute("SELECT set_config('metis.user_id', %s, false)", (str(uid) if uid else "",))
        self.conn.commit()  # persist role + identity so later rollbacks cannot revert to superuser
        assert self.conn.execute("SELECT current_user").fetchone()[0] == "metis_app"
        self.conn.commit()
        return self.conn


@pytest.fixture
def app(pg, su):
    a = App(pg)
    yield a
    a.conn.rollback()
    a.conn.close()
