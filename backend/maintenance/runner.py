"""Maintenance runner: calls the migration-0004 purge functions and logs only counts.

    python -m backend.maintenance                 run once, exit 0 / non-zero on error
    python -m backend.maintenance --loop 3600     run every 3600 s until stopped

Connects as the app role (METIS_DATABASE_URL). The functions enforce their own safety floors in the database
(rate buckets older than 2 h, expired revocations, never-verified sign-ups older than the given age, min
1 day), so this process cannot widen what it deletes. Logs never contain emails, ids or any user data.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Sequence
from datetime import timedelta
from types import FrameType

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from backend.core.db import make_engine, make_session_factory, request_session

log = logging.getLogger("metis.maintenance")

UNVERIFIED_MAX_AGE = timedelta(days=7)
MIN_LOOP_INTERVAL_S = 60

_STEPS: tuple[tuple[str, str], ...] = (
    ("rate_limit_buckets", "SELECT rate_limit_purge()"),
    ("session_revocations", "SELECT session_purge_expired()"),
    ("unverified_accounts", "SELECT auth_purge_unverified(make_interval(secs => :age))"),
)


def run_once(factory: sessionmaker[Session]) -> dict[str, int]:
    """Run every purge in its own transaction (one failing step does not hide the others) and return counts.

    Raises RuntimeError after all steps ran if any step failed; the message carries step names only.
    """
    counts: dict[str, int] = {}
    failed: list[str] = []
    for name, sql in _STEPS:
        try:
            with request_session(factory, None) as s:
                n = s.execute(text(sql), {"age": UNVERIFIED_MAX_AGE.total_seconds()} if ":age" in sql else {})
                counts[name] = int(n.scalar_one() or 0)
        except Exception as exc:  # noqa: BLE001 - class name only: DB messages can carry row data
            failed.append(name)
            log.error("maintenance step failed: %s (%s)", name, type(exc).__name__)
    log.info("maintenance done: %s", " ".join(f"{k}={v}" for k, v in counts.items()))
    if failed:
        raise RuntimeError("maintenance steps failed: " + ", ".join(failed))
    return counts


def _loop(factory: sessionmaker[Session], interval_s: int) -> int:
    stop = threading.Event()

    def _halt(_sig: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _halt)
    signal.signal(signal.SIGINT, _halt)
    rc = 0
    while not stop.is_set():
        try:
            run_once(factory)
            rc = 0
        except Exception:  # noqa: BLE001 - already logged by name; keep looping, report the last outcome
            rc = 1
        stop.wait(interval_s)
    return rc


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backend.maintenance", description=__doc__.split("\n")[0])
    ap.add_argument(
        "--loop", type=int, metavar="SECONDS", help=f"repeat every N seconds (min {MIN_LOOP_INTERVAL_S})"
    )
    args = ap.parse_args(argv)
    if args.loop is not None and args.loop < MIN_LOOP_INTERVAL_S:
        ap.error(f"--loop must be at least {MIN_LOOP_INTERVAL_S} seconds")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        factory = make_session_factory(make_engine())
    except Exception as exc:  # noqa: BLE001 - settings errors are names-only by design; never echo the URL
        log.error("cannot start: %s", type(exc).__name__)
        return 2
    if args.loop is not None:
        return _loop(factory, args.loop)
    try:
        run_once(factory)
    except Exception:  # noqa: BLE001 - logged by run_once
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
