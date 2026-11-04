"""Job queue interface and an in-process implementation.

State is held in memory only (lost on restart); RESULTS are persisted by the job function to the prediction
table. Durable jobs need a migration (job table) and a worker process: see the Agent 6 handoff.
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
    def submit(self, owner_id: uuid.UUID, project_id: uuid.UUID, total: int, estimate_seconds: float, fn: JobFn) -> JobRecord: ...
    def get(self, owner_id: uuid.UUID, job_id: uuid.UUID) -> Optional[JobRecord]: ...


class InProcessJobQueue:
    def __init__(self, max_workers: int = 2) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="metis-job")
        self._jobs: OrderedDict[uuid.UUID, JobRecord] = OrderedDict()
        self._futures: dict[uuid.UUID, object] = {}
        self._lock = threading.Lock()

    def submit(self, owner_id: uuid.UUID, project_id: uuid.UUID, total: int, estimate_seconds: float, fn: JobFn) -> JobRecord:
        rec = JobRecord(uuid.uuid4(), owner_id, project_id, total, estimate_seconds=estimate_seconds)
        with self._lock:
            self._jobs[rec.id] = rec
            while len(self._jobs) > MAX_TRACKED:
                oldest = next(iter(self._jobs))
                if self._jobs[oldest].state in ("queued", "running"):
                    break
                self._jobs.pop(oldest)
                self._futures.pop(oldest, None)

        def run() -> None:
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

        self._futures[rec.id] = self._pool.submit(run)
        return rec

    def get(self, owner_id: uuid.UUID, job_id: uuid.UUID) -> Optional[JobRecord]:
        with self._lock:
            rec = self._jobs.get(job_id)
        return rec if rec is not None and rec.owner_id == owner_id else None

    def wait(self, job_id: uuid.UUID, timeout: float = 60.0) -> None:
        fut = self._futures.get(job_id)
        if fut is not None:
            fut.result(timeout=timeout)  # type: ignore[attr-defined]
