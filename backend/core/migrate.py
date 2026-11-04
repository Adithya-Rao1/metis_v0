"""Tiny migration runner: applies backend/db/migrations/*.sql in order, idempotently.

Usage: METIS_MIGRATION_URL=postgresql://owner@host/db python -m backend.core.migrate
Must run as the migration/owner role, never the app role.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import sys

import psycopg

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations"

_LOCK_ID = 727001

_ENSURE = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now(), checksum text)"
)
# Backward compatible with tables created before checksums existed.
_ENSURE_CHECKSUM = "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum text"


class MigrationError(RuntimeError):
    """An applied migration changed, or a new migration sorts before applied ones."""


def _checksum(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_migrations(uri: str, directory: pathlib.Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending files in lexical order, each in its own transaction. Returns files applied.

    Safe against concurrent runners (the advisory lock is taken before the bookkeeping table is touched).
    Records a sha256 per applied file; refuses to run if an applied file was edited or a new file sorts
    before already-applied ones.
    """
    applied: list[str] = []
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
        try:
            conn.execute(_ENSURE)
            conn.execute(_ENSURE_CHECKSUM)
            done = {r[0]: r[1] for r in conn.execute("SELECT filename, checksum FROM schema_migrations")}
            files = sorted(directory.glob("*.sql"))
            for path in files:
                if path.name in done:
                    if done[path.name] is None:  # legacy row from before checksums: trust on first sight
                        conn.execute(
                            "UPDATE schema_migrations SET checksum = %s WHERE filename = %s",
                            (_checksum(path), path.name),
                        )
                    elif done[path.name] != _checksum(path):
                        raise MigrationError(
                            f"applied migration {path.name} was modified (checksum mismatch)"
                        )
            newest = max(done, default=None)
            for path in files:
                if path.name in done:
                    continue
                if newest is not None and path.name < newest:
                    raise MigrationError(f"migration {path.name} sorts before already-applied {newest}")
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute(
                        "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                        (path.name, _checksum(path)),
                    )
                applied.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
    return applied


def main() -> int:
    uri = os.environ.get("METIS_MIGRATION_URL")
    if not uri:
        print("METIS_MIGRATION_URL is not set", file=sys.stderr)
        return 2
    try:
        applied = apply_migrations(uri)
    except MigrationError as exc:
        print(f"migration refused: {exc}", file=sys.stderr)
        return 3
    print(f"applied {len(applied)} migration(s): {', '.join(applied) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
