"""Tiny migration runner: applies backend/db/migrations/*.sql in order, idempotently.

Usage: METIS_MIGRATION_URL=postgresql://owner@host/db python -m backend.core.migrate
Must run as the migration/owner role, never the app role.
"""

from __future__ import annotations

import os
import pathlib
import sys

import psycopg

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations"

_ENSURE = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
)


def apply_migrations(uri: str, directory: pathlib.Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending files in lexical order, each in its own transaction. Returns files applied."""
    applied: list[str] = []
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute(_ENSURE)
        # advisory lock so concurrent deploys do not race
        conn.execute("SELECT pg_advisory_lock(%s)", (727001,))
        try:
            done = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations")}
            for path in sorted(directory.glob("*.sql")):
                if path.name in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
                applied.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (727001,))
    return applied


def main() -> int:
    uri = os.environ.get("METIS_MIGRATION_URL")
    if not uri:
        print("METIS_MIGRATION_URL is not set", file=sys.stderr)
        return 2
    applied = apply_migrations(uri)
    print(f"applied {len(applied)} migration(s): {', '.join(applied) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
