"""One worker thread, bounded queue, per-job timeout, progress. Jobs are scoped to their owner."""

from __future__ import annotations

import queue
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

MAX_QUEUE = 3
JOB_TIMEOUT_S = 90.0
MAX_KEPT = 100


class QueueFull(Exception):
    pass


class JobManager:
    def __init__(
        self, runner: Callable[..., dict], timeout_s: float = JOB_TIMEOUT_S, max_queue: int = MAX_QUEUE
    ):
        self.runner, self.timeout_s, self.max_queue = runner, timeout_s, max_queue
        self.jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.q: queue.Queue[str] = queue.Queue()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def _ensure_worker(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._loop, name="adapt-worker", daemon=True)
            self.thread.start()

    def submit(self, owner: str, payload: Any) -> str:
        with self.lock:
            if sum(1 for j in self.jobs.values() if j["status"] == "queued") >= self.max_queue:
                raise QueueFull(f"Adaptation queue is full ({self.max_queue} waiting); try again shortly.")
            jid = secrets.token_urlsafe(9)
            self.jobs[jid] = {
                "job_id": jid,
                "owner": owner,
                "status": "queued",
                "stage": "queued",
                "progress": 0.0,
                "result": None,
                "error": None,
                "payload": payload,
                "created": time.monotonic(),
            }
            while len(self.jobs) > MAX_KEPT:
                for k, j in self.jobs.items():
                    if j["status"] in ("done", "failed"):
                        del self.jobs[k]
                        break
                else:
                    break
            self.q.put(jid)
            self._ensure_worker()
        return jid

    def forget_owner(self, owner: str) -> int:
        """Drop the owner's jobs that are not running (account deletion); a running one is flagged and unreadable."""
        with self.lock:
            mine = [k for k, j in self.jobs.items() if j["owner"] == owner]
            for k in mine:
                j = self.jobs[k]
                j["payload"], j["result"] = None, None
                if j["status"] != "running":
                    del self.jobs[k]
            return len(mine)

    def get(self, jid: str, owner: str) -> dict | None:
        j = self.jobs.get(jid)
        if j is None or j["owner"] != owner:
            return None
        return {k: j[k] for k in ("job_id", "status", "stage", "progress", "result", "error")}

    def _loop(self) -> None:
        while True:
            jid = self.q.get()
            j = self.jobs.get(jid)
            if j is None:
                continue
            j["status"], t0 = "running", time.monotonic()

            def prog(stage: str, f: float, j=j) -> None:
                j["stage"], j["progress"] = stage, round(max(0.0, min(1.0, f)), 3)

            try:
                res = self.runner(j["payload"], j["owner"], progress=prog)
                if time.monotonic() - t0 > self.timeout_s:
                    raise TimeoutError(
                        f"Adaptation exceeded the {self.timeout_s:.0f}s limit; result discarded."
                    )
                j["result"], j["status"], j["progress"] = res, "done", 1.0
            except Exception as exc:  # surfaced to the user as a message, never a traceback
                j["status"], j["error"] = "failed", str(exc) or type(exc).__name__
            finally:
                j["payload"] = None
