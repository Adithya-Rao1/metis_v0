"""Database engine and per-request session factory.

Security properties (see docs/THREAT_MODEL.md, docs/DB_SCHEMA.md):
  * The engine connects as the least-privilege app login role (a member of metis_app), never a superuser.
  * Request identity is set ONLY via `SELECT set_config('metis.user_id', :uid, true)` (SET LOCAL semantics),
    inside the request transaction. Nothing is set at session/connection scope.
  * `DISCARD ALL` runs whenever a connection is returned to the pool, so no state can leak between requests.
  * All SQL is parameterized.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from backend.core.settings import Settings, get_settings

_SET_IDENTITY = text("SELECT set_config('metis.user_id', :uid, true)")


def _discard_all(dbapi_conn: Any, _record: Any) -> None:
    """Pool check-in hook: wipe session state. DISCARD ALL cannot run inside a transaction block."""
    previous = dbapi_conn.autocommit
    try:
        dbapi_conn.rollback()
        dbapi_conn.autocommit = True
        dbapi_conn.execute("DISCARD ALL")
    finally:
        dbapi_conn.autocommit = previous


def make_engine(url: str | None = None, settings: Settings | None = None, **kwargs: Any) -> Engine:
    settings = settings or get_settings()
    if url is None:
        if settings.database_url is None:
            raise RuntimeError("METIS_DATABASE_URL is not set")
        url = settings.database_url.get_secret_value()
    engine = create_engine(
        url,
        pool_size=settings.db_pool_size,
        pool_pre_ping=True,
        connect_args={"options": f"-c statement_timeout={int(settings.db_statement_timeout_ms)}"},
        **kwargs,
    )
    event.listen(engine, "checkin", _discard_all)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, autoflush=False)


@contextmanager
def request_session(factory: sessionmaker[Session], user_id: uuid.UUID | str | None) -> Iterator[Session]:
    """One transaction per request. Identity is transaction-local; anonymous requests get an empty id."""
    uid = str(user_id) if user_id else ""
    if uid:
        uuid.UUID(uid)  # reject anything that is not a UUID before it reaches the database
    with factory() as session, session.begin():
        session.execute(_SET_IDENTITY, {"uid": uid})
        yield session
