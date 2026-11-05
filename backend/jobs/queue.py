"""Job queue interface and an in-process implementation with durable job records.

Jobs still EXECUTE in this process (a thread pool). When the queue is given a session factory it also writes job
state to the `job` table (migration 0003: owner RLS, composite FK to project), so `GET /jobs/{id}` survives a
restart. RESULTS are persisted by the job function to the prediction table; each reported prediction is linked
to its job (`prediction.job_id`), which is how a finished job's `prediction_ids` are recovered after a restart.
Without a factory (unit tests of the queue itself) state is memory-only, as before.

Ownership and restart behaviour (migration 0006, F-126): every queue object has a random `worker_id`, stamps it on
the rows it creates and refreshes their `heartbeat_at` every `heartbeat_s` while it still tracks unfinished jobs. A
process that dies leaves its rows `queued`/`running` with a heartbeat that stops advancing. The app role cannot see
other users' rows, so there is no global sweep; whenever an owner touches the queue (`submit` or a `get` that is not
served from memory) that owner's `queued`/`running` rows are marked `failed` (`interrupted by restart`) only if this
process does not track them AND their heartbeat is older than `stale_s` (or they carry no worker or heartbeat, which
only rows from before the migration do). A second API process (second worker, rolling restart) therefore never fails
a live job, and a job's final write only lands on a row that is still queued/running and still belongs to this worker.

Never put structures or SMILES in job rows or logs: errors are stored as fixed codes (exception class names only).

Bounds: finished records are evicted from memory oldest-first wherever they sit (the DB row stays); a hard cap on
tracked in-memory records; a per-user quota of queued/running jobs (`max_active`, counted from the DB when
durable; the route answers 429). Jobs are owner-scoped: `get` returns None for anyone but the owner (404).
"""

from __future__ import annotations

import logging
import threading
import uuid
import weakref
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
HEARTBEAT_S = 15.0  # how often a live queue refreshes the rows it runs
STALE_S = 60.0  # a row whose heartbeat is older than this has no live owner process (four missed beats)

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


def _heartbeat_loop(
    ref: weakref.ReferenceType[InProcessJobQueue], stop: threading.Event, interval: float
) -> None:
    """Daemon thread body. Holds only a weak reference so an abandoned queue (and this thread) can be collected."""
    while not stop.wait(interval):
        q = ref()
        if q is None:
            return
        try:
            q._beat_once()
        except Exception as exc:  # noqa: BLE001 - the heartbeat must never die
            log.warning("job heartbeat failed (%s)", type(exc).__name__)
        del q


class InProcessJobQueue:
    def __init__(
        self,
        max_workers: int = 2,
        factory: Optional[sessionmaker[Session]] = None,
        heartbeat_s: float = HEARTBEAT_S,
        stale_s: float = STALE_S,
    ) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="metis-job")
        self._jobs: OrderedDict[uuid.UUID, JobRecord] = OrderedDict()
        self._futures: dict[uuid.UUID, object] = {}
        self._lock = threading.Lock()
        self._factory = factory
        self.worker_id = uuid.uuid4()
        self._heartbeat_s = heartbeat_s
        self._stale_s = stale_s
        self._stop = threading.Event()
        self._beat: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ heartbeat
    def _start_heartbeat(self) -> None:
        """Caller holds the lock. Start (once) the daemon thread that keeps this process's unfinished rows alive."""
        if self._factory is None or (self._beat is not None and self._beat.is_alive()):
            return
        self._beat = threading.Thread(
            target=_heartbeat_loop,
            args=(weakref.ref(self), self._stop, self._heartbeat_s),
            daemon=True,
            name="metis-job-beat",
        )
        self._beat.start()

    def _beat_once(self) -> None:
        """Refresh the heartbeat of every unfinished job this process tracks (one short transaction per owner)."""
        with self._lock:
            by_owner: dict[uuid.UUID, list[uuid.UUID]] = {}
            for jid, r in self._jobs.items():
                if r.state in ("queued", "running"):
                    by_owner.setdefault(r.owner_id, []).append(jid)
        for owner_id, ids in by_owner.items():
            self._write(
                owner_id,
                "UPDATE job SET heartbeat_at = now() WHERE owner_id = :u AND worker_id = :w "
                "AND state IN ('queued','running') AND id = ANY(CAST(:ids AS uuid[]))",
                {"u": owner_id, "w": self.worker_id, "ids": ids},
            )

    def _memory_active(self, owner_id: uuid.UUID) -> int:
        """Caller holds the lock. This owner's queued/running jobs that this process still tracks."""
        return sum(
            1 for j in self._jobs.values() if j.owner_id == owner_id and j.state in ("queued", "running")
        )

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
        """Caller holds the lock. Fail this owner's queued/running rows that this process does not track AND whose owner
        process is gone: no worker or heartbeat recorded (rows from before migration 0006), or a heartbeat older than the
        stale limit. A job that another live process runs keeps a fresh heartbeat and is left alone."""
        live = [
            j for j, r in self._jobs.items() if r.owner_id == owner_id and r.state in ("queued", "running")
        ]
        stmt = text(
            "UPDATE job SET state = 'failed', error_code = :code, finished_at = now() "
            "WHERE owner_id = :u AND state IN ('queued','running') AND NOT (id = ANY(CAST(:live AS uuid[]))) "
            "AND (worker_id IS NULL OR heartbeat_at IS NULL OR worker_id = :w "
            "     OR heartbeat_at < now() - make_interval(secs => :stale))"
        )
        s.execute(
            stmt,
            {
                "code": ERR_INTERRUPTED,
                "u": owner_id,
                "live": live,
                "w": self.worker_id,
                "stale": self._stale_s,
            },
        )

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
                        db_active = s.execute(
                            text(
                                "SELECT count(*) FROM job WHERE owner_id = :u AND state IN ('queued','running')"
                            ),
                            {"u": owner_id},
                        ).scalar_one()
                        # Counted by owner from both sources: deleting a project cascades its job rows away while
                        # their threads still run, so the rows alone would under-count (F-127).
                        if max(db_active, self._memory_active(owner_id)) >= max_active:
                            raise QuotaExceeded()
                    s.execute(
                        text(
                            "INSERT INTO job (id, owner_id, project_id, kind, estimated_seconds, worker_id, heartbeat_at) "
                            "VALUES (:id, :u, :p, 'predict', :est, :w, now())"
                        ),
                        {
                            "id": rec.id,
                            "u": owner_id,
                            "p": project_id,
                            "est": max(0.0, float(estimate_seconds)),
                            "w": self.worker_id,
                        },
                    )
                self._start_heartbeat()
            elif max_active is not None and self._memory_active(owner_id) >= max_active:
                raise QuotaExceeded()
            self._jobs[rec.id] = rec
            self._evict()

        def run() -> None:
            if rec.state == "failed":  # dropped while queued (overload)
                return
            rec.state = "running"
            self._write(
                owner_id,
                "UPDATE job SET state = 'running', started_at = now(), heartbeat_at = now() "
                "WHERE id = :id AND owner_id = :u AND worker_id = :w AND state IN ('queued','running')",
                {"id": rec.id, "u": owner_id, "w": self.worker_id},
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
                    "UPDATE job SET state = 'succeeded', progress = 1, finished_at = now() "
                    "WHERE id = :id AND owner_id = :u AND worker_id = :w AND state IN ('queued','running')",
                    {"id": rec.id, "u": owner_id, "w": self.worker_id},
                )
            except Exception as exc:  # noqa: BLE001 - class name only: messages may carry structure content
                rec.state = "failed"
                rec.error = f"job failed ({type(exc).__name__})"
                self._write(
                    owner_id,
                    "UPDATE job SET state = 'failed', error_code = :code, finished_at = now() "
                    "WHERE id = :id AND owner_id = :u AND worker_id = :w AND state IN ('queued','running')",
                    {
                        "id": rec.id,
                        "u": owner_id,
                        "w": self.worker_id,
                        "code": (_ERR_FAILED_PREFIX + type(exc).__name__)[:64],
                    },
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
        if wait:  # keep the heartbeat going for jobs that are still running when the caller does not wait
            self._stop.set()

    def wait(self, job_id: uuid.UUID, timeout: float = 60.0) -> None:
        fut = self._futures.get(job_id)
        if fut is not None:
            fut.result(timeout=timeout)  # type: ignore[attr-defined]
