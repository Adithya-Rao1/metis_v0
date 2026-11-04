import os

import psycopg
import pytest
from sqlalchemy.engine import make_url

from backend.core.migrate import apply_migrations


@pytest.fixture(scope="session")
def pg(tmp_path_factory):
    """Superuser URI with the baseline applied via the migration runner.

    Uses METIS_TEST_PG_URI (CI Postgres service) when set, otherwise an embedded pgserver.
    """
    external = os.environ.get("METIS_TEST_PG_URI")
    if external:
        uri, srv = external, None
    else:
        import pgserver

        srv = pgserver.get_server(tmp_path_factory.mktemp("pgf"), cleanup_mode="stop")
        uri = srv.get_uri()
    apply_migrations(uri)
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS metis_app_login")
        c.execute("CREATE ROLE metis_app_login LOGIN PASSWORD 'test-only' IN ROLE metis_app")
    yield uri
    if srv:
        srv.cleanup()


@pytest.fixture(scope="session")
def app_url(pg) -> str:
    """SQLAlchemy URL that logs in as the least-privilege app login role."""
    u = make_url(pg.replace("postgresql://", "postgresql+psycopg://", 1))
    return u.set(username="metis_app_login", password="test-only").render_as_string(hide_password=False)
