import uuid

import psycopg
import pytest
from sqlalchemy import text

from backend.core.db import make_engine, make_session_factory, request_session
from backend.core.migrate import apply_migrations
from backend.core.settings import Settings


@pytest.fixture
def engine(app_url):
    eng = make_engine(app_url, settings=Settings(env="test", db_pool_size=1))
    yield eng
    eng.dispose()


def test_connects_as_least_privilege_role(engine):
    with engine.connect() as c:
        row = c.execute(
            text("SELECT current_user, (SELECT rolsuper FROM pg_roles WHERE rolname=current_user)")
        ).one()
    assert row[0] == "metis_app_login" and row[1] is False


def test_identity_is_transaction_local(engine):
    f = make_session_factory(engine)
    uid = uuid.uuid4()
    with request_session(f, uid) as s:
        assert s.execute(text("SELECT current_user_id()")).scalar() == uid
    # same physical connection (pool_size=1) must not retain the identity
    with f() as s, s.begin():
        assert s.execute(text("SELECT current_user_id()")).scalar() is None


def test_anonymous_request_has_no_identity(engine):
    with request_session(make_session_factory(engine), None) as s:
        assert s.execute(text("SELECT current_user_id()")).scalar() is None


def test_identity_does_not_leak_between_requests(engine):
    f = make_session_factory(engine)
    a, b = uuid.uuid4(), uuid.uuid4()
    pids = []
    for u in (a, b, None):
        with request_session(f, u) as s:
            pids.append(s.execute(text("SELECT pg_backend_pid()")).scalar())
            assert s.execute(text("SELECT current_user_id()")).scalar() == u
    assert len(set(pids)) == 1  # connection was reused, yet no leak


def test_rollback_also_clears_identity(engine):
    f = make_session_factory(engine)
    with pytest.raises(RuntimeError), request_session(f, uuid.uuid4()):
        raise RuntimeError("boom")
    with f() as s, s.begin():
        assert s.execute(text("SELECT current_user_id()")).scalar() is None


def test_session_scope_state_discarded_on_checkin(engine):
    with engine.connect() as c:
        c.execute(text("SET application_name = 'leaky'"))
        c.execute(text("SELECT set_config('metis.user_id', :u, false)"), {"u": str(uuid.uuid4())})
        c.commit()
    with engine.connect() as c:
        assert c.execute(text("SHOW application_name")).scalar() != "leaky"
        assert c.execute(text("SELECT current_user_id()")).scalar() is None


def test_rejects_non_uuid_identity(engine):
    with (
        pytest.raises(ValueError),
        request_session(make_session_factory(engine), "x'; DROP TABLE app_user;--"),
    ):
        pass


def test_app_role_cannot_ddl(engine):
    with pytest.raises(Exception), engine.begin() as c:
        c.execute(text("CREATE TABLE evil (i int)"))


def test_migration_runner_idempotent(pg):
    assert apply_migrations(pg) == []
    with psycopg.connect(pg) as c:
        names = [r[0] for r in c.execute("SELECT filename FROM schema_migrations ORDER BY 1")]
    assert names[0] == "0001_baseline.sql"
