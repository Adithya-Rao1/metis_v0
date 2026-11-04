"""Job queue interface and an in-process implementation.

State is held in memory only (lost on restart); RESULTS are persisted by the job function to the prediction
table. The `job` table from migration 0003 is not yet wired up (durable jobs need a separate worker process).
Bounds: finished records are evicted oldest-first wherever they sit; a hard cap on tracked records; a per-user
quota of queued/running jobs (`max_active`, the route answers 429).
Jobs are owner-scoped: `get` returns None for anyone but the owner (callers answer 404).
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional, Protocol

State = Literal["queued", "running", "done", "failed"]
MAX_TRACKED = 500
MAX_ACTIVE_PER_USER = 10


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
    def submit(self, owner_id: uuid.UUID, project_id: uuid.UUID, total: int, estimate_seconds: float, fn: JobFn, max_active: Optional[int] = None) -> JobRecord: ...
    def get(self, owner_id: uuid.UUID, job_id: uuid.UUID) -> Optional[JobRecord]: ...


class InProcessJobQueue:
    def __init__(self, max_workers: int = 2) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="metis-job")
        self._jobs: OrderedDict[uuid.UUID, JobRecord] = OrderedDict()
        self._futures: dict[uuid.UUID, object] = {}
        self._lock = threading.Lock()

    def submit(self, owner_id: uuid.UUID, project_id: uuid.UUID, total: int, estimate_seconds: float, fn: JobFn, max_active: Optional[int] = None) -> JobRecord:
        rec = JobRecord(uuid.uuid4(), owner_id, project_id, total, estimate_seconds=estimate_seconds)
        with self._lock:
            if max_active is not None and sum(1 for j in self._jobs.values() if j.owner_id == owner_id and j.state in ("queued", "running")) >= max_active:
                raise QuotaExceeded()
            self._jobs[rec.id] = rec
            self._evict()

        def run() -> None:
            if rec.state == "failed":  # dropped while queued (overload)
                return
            rec.state = "running"
            try:
                def progress(pid: uuid.UUID) -> None:
                    rec.done += 1
                    rec.prediction_ids.append(pid)

                fn(progress)
                rec.state = "done"
            except Exception as exc:  # noqa: BLE001 - class name only: messages may carry structure content
                rec.state = "failed"
                rec.error = f"job failed ({type(exc).__name__})"

        with self._lock:
            self._futures[rec.id] = self._pool.submit(run)
        return rec

    def _evict(self) -> None:
        """Caller holds the lock. Drop finished records oldest-first (regardless of unfinished ones ahead of them);
        if still over the hard cap, drop the oldest still-queued jobs (cancelled, never started) as a last resort."""
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
            self._jobs[jid].state = "failed"
            self._jobs[jid].error = "job dropped (server overloaded)"
            self._jobs.pop(jid)
            self._futures.pop(jid, None)

    def get(self, owner_id: uuid.UUID, job_id: uuid.UUID) -> Optional[JobRecord]:
        with self._lock:
            rec = self._jobs.get(job_id)
        return rec if rec is not None and rec.owner_id == owner_id else None

    def shutdown(self, wait: bool = True) -> None:
        """Stop accepting work and (by default) let queued and running jobs finish."""
        self._pool.shutdown(wait=wait)

    def wait(self, job_id: uuid.UUID, timeout: float = 60.0) -> None:
        fut = self._futures.get(job_id)
        if fut is not None:
            fut.result(timeout=timeout)  # type: ignore[attr-defined]
