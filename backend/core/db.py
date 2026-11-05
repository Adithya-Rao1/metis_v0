"""Database engine and per-request session factory.

Security properties (see docs/THREAT_MODEL.md, docs/DB_SCHEMA.md):
  * The engine connects as the least-privilege app login role (a member of metis_app), never a superuser.
  * Request identity is set ONLY via `SELECT set_config('metis.user_id', :uid, true)` (SET LOCAL semantics),
    inside the request transaction. Nothing is set at session/connection scope.
  * `DISCARD ALL` runs whenever a connection is returned to the pool, so no state can leak between requests.
  * All SQL is parameterized.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from backend.core.settings import DbSettings, Settings, get_settings

_CANONICAL_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

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


def make_engine(
    url: str | None = None, settings: Settings | DbSettings | None = None, **kwargs: Any
) -> Engine:
    settings = settings or get_settings()
    if url is None:
        if settings.database_url is None:
            raise RuntimeError("METIS_DATABASE_URL is not set")
        url = settings.database_url.get_secret_value()
    engine = create_engine(
        url,
        pool_size=settings.db_pool_size,
        pool_pre_ping=True,
        connect_args={
            "options": f"-c statement_timeout={int(settings.db_statement_timeout_ms)}",
            # DISCARD ALL on check-in deallocates server-side prepared statements, so psycopg must never
            # auto-prepare (its client-side cache would then reference statements that no longer exist).
            "prepare_threshold": None,
        },
        **kwargs,
    )
    event.listen(engine, "checkin", _discard_all)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, autoflush=False)


def _canonical_user_id(user_id: uuid.UUID | str | None) -> str:
    """Return the canonical lowercase hyphenated UUID, "" for anonymous; reject everything else."""
    if user_id is None or user_id == "":
        return ""
    if isinstance(user_id, uuid.UUID):
        return str(user_id)
    if not isinstance(user_id, str):
        raise TypeError("user_id must be a uuid.UUID, a UUID string or None")
    if not _CANONICAL_UUID.fullmatch(user_id) or not user_id.isascii():
        raise ValueError("user_id is not a canonical UUID")
    return str(uuid.UUID(user_id))


@contextmanager
def request_session(factory: sessionmaker[Session], user_id: uuid.UUID | str | None) -> Iterator[Session]:
    """One transaction per request. Identity is transaction-local; anonymous requests get an empty id."""
    uid = _canonical_user_id(user_id)
    with factory() as session, session.begin():
        session.execute(_SET_IDENTITY, {"uid": uid})
        yield session
