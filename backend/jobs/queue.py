"""Job queue interface and an in-process implementation with durable job records.

Jobs still EXECUTE in this process (a thread pool). When the queue is given a session factory it also writes job
state to the `job` table (migration 0003: owner RLS, composite FK to project), so `GET /jobs/{id}` survives a
restart. RESULTS are persisted by the job function to the prediction table; each reported prediction is linked
to its job (`prediction.job_id`), which is how a finished job's `prediction_ids` are recovered after a restart.
Without a factory (unit tests of the queue itself) state is memory-only, as before.

Restart behaviour: a process that dies leaves its rows `queued`/`running`. The app role cannot see other users'
rows, so there is no global sweep; instead, whenever an owner touches the queue (`submit` or a `get` that is not
served from memory) that owner's `queued`/`running` rows that this process is not tracking are marked `failed`
with the reason `interrupted by restart`. This assumes ONE API process owns the queue (see docs/OPERATIONS.md);
a second process would have its live jobs reaped by the first. SCHEMA-REQUEST for a global startup sweep: see
the Wave 3 backend handoff.

Never put structures or SMILES in job rows or logs: errors are stored as fixed codes (exception class names only).

Bounds: finished records are evicted from memory oldest-first wherever they sit (the DB row stays); a hard cap on
tracked in-memory records; a per-user quota of queued/running jobs (`max_active`, counted from the DB when
durable; the route answers 429). Jobs are owner-scoped: `get` returns None for anyone but the owner (404).
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from backend.core.db import request_session

log = logging.getLogger("metis.jobs")

State = Literal["queued", "running", "done", "failed"]
MAX_TRACKED = 500
MAX_ACTIVE_PER_USER = 10

# error_code values (the column holds fixed codes only, <= 64 chars) and the plain text the API shows.
ERR_INTERRUPTED = "interrupted_by_restart"
ERR_DROPPED = "dropped_overload"
ERR_CANCELLED = "cancelled"
_ERR_FAILED_PREFIX = "job_failed_"
_ERR_TEXT = {
    ERR_INTERRUPTED: "interrupted by restart",
    ERR_DROPPED: "job dropped (server overloaded)",
    ERR_CANCELLED: "job cancelled",
}
_DB_TO_STATE: dict[str, State] = {
    "queued": "queued",
    "running": "running",
    "succeeded": "done",
    "failed": "failed",
    "cancelled": "failed",
}


class QuotaExceeded(Exception):
    """The owner already has the maximum number of queued/running jobs."""


@dataclass
class JobRecord:
    id: uuid.UUID
    owner_id: uuid.UUID
    project_id: uuid.UUID
    total: int
    done: int = 0
    state: State = "queued"
    error: Optional[str] = None  # generic message only, never structure content
    estimate_seconds: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    prediction_ids: list[uuid.UUID] = field(default_factory=list)


class Progress(Protocol):
    def __call__(self, prediction_id: uuid.UUID) -> None: ...


JobFn = Callable[[Progress], None]


class JobQueue(Protocol):
    def submit(
        self,
        owner_id: uuid.UUID,
        project_id: uuid.UUID,
        total: int,
        estimate_seconds: float,
        fn: JobFn,
        max_active: Optional[int] = None,
    ) -> JobRecord: ...
    def get(self, owner_id: uuid.UUID, job_id: uuid.UUID) -> Optional[JobRecord]: ...


def _error_text(code: Optional[str]) -> Optional[str]:
    if code is None:
        return None
    if code.startswith(_ERR_FAILED_PREFIX):
        return f"job failed ({code[len(_ERR_FAILED_PREFIX) :]})"
    return _ERR_TEXT.get(code, "job failed")


class InProcessJobQueue:
    def __init__(self, max_workers: int = 2, factory: Optional[sessionmaker[Session]] = None) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="metis-job")
        self._jobs: OrderedDict[uuid.UUID, JobRecord] = OrderedDict()
        self._futures: dict[uuid.UUID, object] = {}
        self._lock = threading.Lock()
        self._factory = factory

    # ------------------------------------------------------------------ durable bookkeeping
    def _write(self, owner_id: uuid.UUID, sql: str, params: dict) -> None:
        """Best-effort bookkeeping write: a failure is logged by class name and never fails the job itself."""
        if self._factory is None:
            return
        try:
            with request_session(self._factory, owner_id) as s:
                s.execute(text(sql), params)
        except Exception as exc:  # noqa: BLE001 - class name only: DB messages can carry row data
            log.warning("job bookkeeping write failed (%s)", type(exc).__name__)

    def _reap(self, s: Session, owner_id: uuid.UUID) -> None:
        """Caller holds the lock. Fail this owner's queued/running rows that no thread of THIS process is tracking."""
        live = [
            j for j, r in self._jobs.items() if r.owner_id == owner_id and r.state in ("queued", "running")
        ]
        stmt = text(
            "UPDATE job SET state = 'failed', error_code = :code, finished_at = now() "
            "WHERE owner_id = :u AND state IN ('queued','running') AND NOT (id = ANY(CAST(:live AS uuid[])))"
        )
        s.execute(stmt, {"code": ERR_INTERRUPTED, "u": owner_id, "live": live})

    def submit(
        self,
        owner_id: uuid.UUID,
        project_id: uuid.UUID,
        total: int,
        estimate_seconds: float,
        fn: JobFn,
        max_active: Optional[int] = None,
    ) -> JobRecord:
        rec = JobRecord(uuid.uuid4(), owner_id, project_id, total, estimate_seconds=estimate_seconds)
        with self._lock:
            if self._factory is not None:
                with request_session(self._factory, owner_id) as s:
                    self._reap(s, owner_id)
                    if max_active is not None:
                        active = s.execute(
                            text(
                                "SELECT count(*) FROM job WHERE owner_id = :u AND state IN ('queued','running')"
                            ),
                            {"u": owner_id},
                        ).scalar_one()
                        if active >= max_active:
                            raise QuotaExceeded()
                    s.execute(
                        text(
                            "INSERT INTO job (id, owner_id, project_id, kind, estimated_seconds) "
                            "VALUES (:id, :u, :p, 'predict', :est)"
                        ),
                        {
                            "id": rec.id,
                            "u": owner_id,
                            "p": project_id,
                            "est": max(0.0, float(estimate_seconds)),
                        },
                    )
            elif (
                max_active is not None
                and sum(
                    1
                    for j in self._jobs.values()
                    if j.owner_id == owner_id and j.state in ("queued", "running")
                )
                >= max_active
            ):
                raise QuotaExceeded()
            self._jobs[rec.id] = rec
            self._evict()

        def run() -> None:
            if rec.state == "failed":  # dropped while queued (overload)
                return
            rec.state = "running"
            self._write(
                owner_id,
                "UPDATE job SET state = 'running', started_at = now() WHERE id = :id AND owner_id = :u",
                {"id": rec.id, "u": owner_id},
            )
            try:

                def progress(pid: uuid.UUID) -> None:
                    rec.done += 1
                    rec.prediction_ids.append(pid)
                    if self._factory is not None:
                        self._write(
                            owner_id,
                            "WITH link AS (UPDATE prediction SET job_id = :id WHERE id = :pid AND owner_id = :u RETURNING 1) "
                            "UPDATE job SET progress = LEAST(1.0, :frac) WHERE id = :id AND owner_id = :u",
                            {
                                "id": rec.id,
                                "pid": pid,
                                "u": owner_id,
                                "frac": rec.done / rec.total if rec.total > 0 else 0.0,
                            },
                        )

                fn(progress)
                rec.state = "done"
                self._write(
                    owner_id,
                    "UPDATE job SET state = 'succeeded', progress = 1, finished_at = now() WHERE id = :id AND owner_id = :u",
                    {"id": rec.id, "u": owner_id},
                )
            except Exception as exc:  # noqa: BLE001 - class name only: messages may carry structure content
                rec.state = "failed"
                rec.error = f"job failed ({type(exc).__name__})"
                self._write(
                    owner_id,
                    "UPDATE job SET state = 'failed', error_code = :code, finished_at = now() WHERE id = :id AND owner_id = :u",
                    {"id": rec.id, "u": owner_id, "code": (_ERR_FAILED_PREFIX + type(exc).__name__)[:64]},
                )

        with self._lock:
            self._futures[rec.id] = self._pool.submit(run)
        return rec

    def _evict(self) -> None:
        """Caller holds the lock. Drop finished records oldest-first (regardless of unfinished ones ahead of them);
        if still over the hard cap, drop the oldest still-queued jobs (cancelled, never started) as a last resort.
        Durable rows of evicted finished jobs stay readable through `get`."""
        if len(self._jobs) <= MAX_TRACKED:
            return
        for jid in [j for j, r in self._jobs.items() if r.state in ("done", "failed")]:
            if len(self._jobs) <= MAX_TRACKED:
                return
            self._jobs.pop(jid)
            self._futures.pop(jid, None)
        for jid in [j for j, r in self._jobs.items() if r.state == "queued"]:
            if len(self._jobs) <= MAX_TRACKED:
                return
            fut = self._futures.get(jid)
            if fut is not None and not fut.cancel():  # type: ignore[attr-defined]
                continue
            dropped = self._jobs[jid]
            dropped.state = "failed"
            dropped.error = _ERR_TEXT[ERR_DROPPED]
            self._write(
                dropped.owner_id,
                "UPDATE job SET state = 'failed', error_code = :code, finished_at = now() WHERE id = :id AND owner_id = :u",
                {"id": jid, "u": dropped.owner_id, "code": ERR_DROPPED},
            )
            self._jobs.pop(jid)
            self._futures.pop(jid, None)

    def get(self, owner_id: uuid.UUID, job_id: uuid.UUID) -> Optional[JobRecord]:
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is not None:
                return rec if rec.owner_id == owner_id else None
            if self._factory is None:
                return None
            with request_session(self._factory, owner_id) as s:
                self._reap(s, owner_id)
                return self._load(s, owner_id, job_id)

    @staticmethod
    def _load(s: Session, owner_id: uuid.UUID, job_id: uuid.UUID) -> Optional[JobRecord]:
        """Rebuild a record from the durable row (owner filter explicit; RLS is the second wall)."""
        row = s.execute(
            text(
                "SELECT project_id, state, progress, error_code, estimated_seconds, created_at FROM job "
                "WHERE id = :id AND owner_id = :u"
            ),
            {"id": job_id, "u": owner_id},
        ).one_or_none()
        if row is None:
            return None
        pids = list(
            s.execute(
                text(
                    "SELECT id FROM prediction WHERE job_id = :id AND owner_id = :u ORDER BY created_at, id"
                ),
                {"id": job_id, "u": owner_id},
            ).scalars()
        )
        done = len(pids)
        # The table stores a progress fraction, not the total: total = done / progress (exact for finished jobs).
        total = done if row.state == "succeeded" else (round(done / row.progress) if row.progress > 0 else 0)
        return JobRecord(
            id=job_id,
            owner_id=owner_id,
            project_id=row.project_id,
            total=total,
            done=done,
            state=_DB_TO_STATE[row.state],
            error=_error_text(row.error_code),
            estimate_seconds=float(row.estimated_seconds or 0.0),
            created_at=row.created_at,
            prediction_ids=pids,
        )

    def shutdown(self, wait: bool = True) -> None:
        """Stop accepting work and (by default) let queued and running jobs finish."""
        self._pool.shutdown(wait=wait)

    def wait(self, job_id: uuid.UUID, timeout: float = 60.0) -> None:
        fut = self._futures.get(job_id)
        if fut is not None:
            fut.result(timeout=timeout)  # type: ignore[attr-defined]
