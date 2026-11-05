"""Killable worker pool for the CPU-heavy, user-input-driven Lead Studio operations.

Molecule validation, manual-mode preview / applicable edits / step checks and SMARTS checks all run RDKit on text a
user typed. A pathological input can hang or crash native code, which no thread can interrupt. So the API process
never runs them: each job goes to one of a few worker processes (`heavychild`, fresh interpreters with a minimal
environment, see `procutil`). A job that exceeds its time limit, or a worker that crashes, is killed and replaced and
the caller gets a plain 422 / `ok: false` answer; the API keeps serving. Workers are recycled after N jobs and exit by
themselves when idle or when the API process is gone.

Admission is non-blocking as before: at most `workers + max_waiting` jobs may be in flight, beyond that a request
gets 429 (Retry-After) at once.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import weakref
from collections import deque
from typing import Any

from fastapi import HTTPException

from leadstudio.server import molio, procutil
from leadstudio.server.core import Rejected

log = logging.getLogger("leadstudio.server")

HEAVY_MODULE = "leadstudio.server.heavychild"


class HeavyFailed(HTTPException):
    """The job did not finish: it ran over its time limit or its worker crashed (the worker was replaced)."""

    def __init__(self, timed_out: bool):
        self.timed_out = timed_out
        msg = "This input took too long to process" if timed_out else "This input could not be processed"
        super().__init__(status_code=422, detail=msg)
        self.message = msg


def _kill_all(workers: set[procutil.Child]) -> None:
    for c in list(workers):
        c.kill()


class _Worker:
    def __init__(self, child: procutil.Child):
        self.child, self.jobs = child, 0


class HeavyPool:
    def __init__(
        self,
        workers: int = 2,
        timeout: float = 20.0,
        recycle_after: int = 200,
        max_waiting: int = 4,
        mem_mb: int = 0,
        module: str = HEAVY_MODULE,
        startup_timeout: float = 60.0,
    ):
        self.workers, self.timeout, self.recycle_after = max(1, workers), timeout, max(1, recycle_after)
        self.mem_mb, self.module, self.startup_timeout = mem_mb, module, startup_timeout
        self._admit = threading.BoundedSemaphore(self.workers + max(0, max_waiting))
        self._cond = threading.Condition()
        self._idle: deque[_Worker] = deque()
        self._count = 0  # workers started and not yet retired
        self._children: set[procutil.Child] = set()
        self._closed = False
        weakref.finalize(self, _kill_all, self._children)

    # ------------------------------------------------------------------ workers
    def _acquire(self, deadline: float) -> _Worker:
        with self._cond:
            while True:
                if self._closed:
                    raise Rejected(503, "Lead Studio is shutting down")
                while self._idle:
                    w = self._idle.popleft()
                    if w.child.alive():
                        return w
                    self._retire_locked(w)
                if self._count < self.workers:
                    self._count += 1
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    raise Rejected(429, "Lead Studio is busy, try again in a moment", 2)
                self._cond.wait(min(left, 0.5))
        try:
            child = procutil.Child.spawn(self.module, [str(self.mem_mb)])
        except OSError:
            with self._cond:
                self._count -= 1
                self._cond.notify()
            raise Rejected(503, "Lead Studio could not start a worker, try again later", 10) from None
        self._children.add(child)
        try:
            ready = child.read_line(self.startup_timeout)  # imports and model/registry loading, not a job
            if ready is None or b"ready" not in ready:
                raise procutil.ChildGone
        except (procutil.ChildGone, procutil.ChildMisbehaved):
            with self._cond:
                self._retire_locked(_Worker(child))
            raise Rejected(503, "Lead Studio could not start a worker, try again later", 10) from None
        return _Worker(child)

    def _retire_locked(self, w: _Worker) -> None:
        w.child.kill()
        self._children.discard(w.child)
        self._count -= 1
        self._cond.notify()

    def _release(self, w: _Worker, healthy: bool) -> None:
        with self._cond:
            if healthy and w.jobs < self.recycle_after and not self._closed:
                self._idle.append(w)
                self._cond.notify()
            else:
                self._retire_locked(w)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        _kill_all(self._children)

    # ------------------------------------------------------------------ jobs
    def call(self, op: str, args: dict[str, Any], timeout: float | None = None) -> Any:
        """Run one job in a worker and return its result. Raises HeavyFailed (time limit / crash), the
        HTTPException the operation itself raised, or Rejected (429 busy)."""
        if not self._admit.acquire(blocking=False):
            raise Rejected(429, "Lead Studio is busy, try again in a moment", 2)
        try:
            limit = timeout or self.timeout
            w = self._acquire(
                time.monotonic() + limit
            )  # waiting for a free worker; the job's own limit starts below
            deadline = time.monotonic() + limit
            healthy, answer = False, None
            try:
                w.child.send(json.dumps({"op": op, "args": args}).encode() + b"\n", limit)
                line = w.child.read_line(max(0.0, deadline - time.monotonic()))
                if line is None:
                    raise HeavyFailed(timed_out=True)
                answer = json.loads(line)
                w.jobs += 1
                healthy = "error" not in answer
            except HeavyFailed:
                log.warning("heavy job %s timed out; worker replaced", op)
                raise
            except (procutil.ChildGone, procutil.ChildMisbehaved, TimeoutError, ValueError) as exc:
                log.warning("heavy job %s failed (%s); worker replaced", op, type(exc).__name__)
                raise HeavyFailed(timed_out=isinstance(exc, TimeoutError)) from None
            finally:
                self._release(w, healthy)
            if "ok" in answer:
                return answer["ok"]
            if "http" in answer:
                raise HTTPException(int(answer["http"][0]), str(answer["http"][1]))
            log.warning("heavy job %s raised %s; worker replaced", op, str(answer.get("error"))[:40])
            raise HeavyFailed(timed_out=False)
        finally:
            self._admit.release()

    # ------------------------------------------------------------------ helpers for the routes
    def parse_many(self, smiles: list[str]) -> list[tuple[str, int]]:
        """(canonical SMILES, heavy atoms) for each input; raises molio.InvalidMolecule for the first bad one."""
        res = self.call("parse", {"items": [{"smiles": s} for s in smiles]})
        out: list[tuple[str, int]] = []
        for r in res:
            if not r["ok"]:
                raise molio.InvalidMolecule(r["error"])
            out.append((r["canonical"], r["heavy"]))
        return out

    def parse(self, smiles: str | None = None, molfile: str | None = None) -> tuple[str, int]:
        res = self.call("parse", {"items": [{"smiles": smiles, "molfile": molfile}]})[0]
        if not res["ok"]:
            raise molio.InvalidMolecule(res["error"])
        return res["canonical"], res["heavy"]
