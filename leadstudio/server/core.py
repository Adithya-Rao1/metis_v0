"""Run management and limits for the Lead Studio service.

Every run belongs to one Metis user (`owner`); every lookup takes the caller's user id and answers "unknown" for
anyone else's run, so user B can never read, stream or cancel user A's run. State is in memory of THIS process
(the service is single-process by design; see docs/OPERATIONS.md). Every live run executes in its own child process
(`runchild`), killed on timeout, cancel, client stop and shutdown; this module only supervises it and relays its
events. Rate limiting of the HTTP routes is done by the
shared database limiter in `backend.accounts.deps.rate_limit`, not here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import typing
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from leadstudio.profiles import ProfileBody
from leadstudio.schema import EventType, RunConfig
from leadstudio.server import procutil

log = logging.getLogger("leadstudio.server")

EVENT_TYPES = frozenset(typing.get_args(EventType))
CHILD_MODULE = "leadstudio.server.runchild"


def _env(name: str, default, cast=float):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    max_concurrent: int = 2  # engine runs executing at once in this process
    max_queue: int = 4  # waiting runs; beyond max_concurrent + max_queue active runs -> 429
    per_user_active: int = 2  # queued + running runs one user may hold
    per_user_streams: int = 4  # open SSE connections one user may hold
    run_timeout: float = (
        90.0  # wall-clock seconds from execution start; the run's child process is killed then
    )
    child_mem_mb: int = (
        6144  # address-space limit of a run's child process (RLIMIT_AS, where the OS has it); 0 = none
    )
    max_run_events: int = 5000  # events one run may produce; beyond this the child is killed
    heavy_workers: int = 2  # processes of the validate / preview / SMARTS worker pool
    heavy_timeout: float = 20.0  # seconds one such job may take before its worker is killed
    heavy_recycle: int = 200  # jobs after which a pool worker is replaced
    max_generations: int = 15
    max_population: int = 40
    max_heavy_atoms: int = 60
    max_heavy_ops: int = 6  # validate / preview / applicable computations running at once in this process
    runs_per_minute: int = 6  # per user, enforced by the shared database limiter
    validates_per_minute: int = 30
    heartbeat: float = 15.0
    orphan_grace: float = 30.0  # a live/mock run nobody listens to for this long is cancelled (client gone)
    mock_delay: float = 0.4  # pacing of the mock replay (seconds / event)
    replay_delay: float = 0.25  # pacing of cache replays
    cache_size: int = 64
    max_runs_kept: int = 200  # finished + active runs held in memory, whole process
    max_runs_per_user_kept: int = 20
    max_live_sessions_per_user: int = 20
    max_live_steps: int = 500
    force_mock: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        d = cls()
        return cls(
            max_concurrent=max(1, _env("STUDIO_MAX_CONCURRENT", d.max_concurrent, int)),
            max_queue=max(0, _env("STUDIO_MAX_QUEUE", d.max_queue, int)),
            per_user_active=max(1, _env("STUDIO_PER_USER_RUNS", d.per_user_active, int)),
            run_timeout=_env("STUDIO_RUN_TIMEOUT", d.run_timeout),
            child_mem_mb=max(0, _env("STUDIO_CHILD_MEM_MB", d.child_mem_mb, int)),
            heavy_workers=max(1, _env("STUDIO_HEAVY_WORKERS", d.heavy_workers, int)),
            heavy_timeout=max(1.0, _env("STUDIO_HEAVY_TIMEOUT", d.heavy_timeout)),
            heavy_recycle=max(1, _env("STUDIO_HEAVY_RECYCLE", d.heavy_recycle, int)),
            max_generations=_env("STUDIO_MAX_GENERATIONS", d.max_generations, int),
            max_population=_env("STUDIO_MAX_POPULATION", d.max_population, int),
            runs_per_minute=_env("STUDIO_RUNS_PER_MIN", d.runs_per_minute, int),
            validates_per_minute=_env("STUDIO_VALIDATES_PER_MIN", d.validates_per_minute, int),
            heartbeat=_env("STUDIO_HEARTBEAT", d.heartbeat),
            orphan_grace=max(0.0, _env("STUDIO_ORPHAN_GRACE", d.orphan_grace)),
            mock_delay=_env("STUDIO_MOCK_DELAY", d.mock_delay),
            replay_delay=_env("STUDIO_REPLAY_DELAY", d.replay_delay),
            force_mock=os.environ.get("STUDIO_FORCE_MOCK", "") not in ("", "0", "false"),
        )


class Rejected(HTTPException):
    """An HTTP error with an optional Retry-After; being an HTTPException it needs no app-level handler."""

    def __init__(self, status: int, message: str, retry_after: int | None = None):
        super().__init__(
            status_code=status,
            detail=message,
            headers={"Retry-After": str(retry_after)} if retry_after else None,
        )
        self.message = message


class SlotPool:
    """Runs at most `workers` supervisor tasks at once, each on its own daemon thread. A task is one run's
    supervisor: it spends its life waiting for the run's child process, which is killed on timeout, cancel, client
    stop and shutdown, so a thread never outlives its run by more than a poll interval."""

    def __init__(self, workers: int, name: str = "lead-run"):
        self.workers, self.name = max(1, workers), name
        self._lock = threading.Lock()
        self._free = self.workers
        self._waiting: deque[tuple[Callable[..., None], tuple]] = deque()
        self._threads: set[threading.Thread] = set()
        self._closed = False

    def submit(self, fn: Callable[..., None], *args: Any) -> None:
        with self._lock:
            self._waiting.append((fn, args))
        self._pump()

    def _pump(self) -> None:
        while True:
            with self._lock:
                if self._closed or self._free <= 0 or not self._waiting:
                    return
                fn, args = self._waiting.popleft()
                self._free -= 1
                t = threading.Thread(target=self._run, args=(fn, args), name=self.name, daemon=True)
                self._threads.add(t)
            t.start()

    def _run(self, fn: Callable[..., None], args: tuple) -> None:
        try:
            fn(*args)
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())
                self._free += 1
            self._pump()

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
            self._waiting.clear()
            threads = list(self._threads)
        if wait:
            for t in threads:
                t.join(timeout=5)


@dataclass
class Run:
    run_id: str
    owner: uuid.UUID
    config: RunConfig
    source: str  # live | mock | replay
    cache_key: str | None = None
    pace: float = 0.0  # delay applied by the SSE writer (replays only)
    profile: ProfileBody | None = (
        None  # the caller's saved scoring profile, live runs only (sent to the child)
    )
    adapter: dict[str, str] | None = (
        None  # the caller's verified adapter {owner, adapter_id, sha256} (live runs)
    )
    events: list[dict] = field(default_factory=list)
    finished: bool = False
    status: str = "queued"  # queued | running | done | error | cancelled
    started: float | None = None
    created: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)
    timer: threading.Timer | None = None
    streams: int = 0  # open SSE connections reading this run
    orphan: threading.Timer | None = None  # armed while nobody reads the run (see RunManager.detach)
    child: procutil.Child | None = None  # the run's process while it executes (live runs)

    def push(self, type_: str, payload: dict) -> bool:
        with self.lock:
            if self.finished:
                return False
            self.events.append(
                {"type": type_, "run_id": self.run_id, "seq": len(self.events), "payload": payload}
            )
            return True

    def finish(self, status: str, error: str | None = None, recoverable: bool = False) -> bool:
        """Terminate the stream exactly once; optionally append a terminal error event."""
        with self.lock:
            if self.finished:
                return False
            if error is not None:
                self.events.append(
                    {
                        "type": "error",
                        "run_id": self.run_id,
                        "seq": len(self.events),
                        "payload": {"message": error, "recoverable": recoverable},
                    }
                )
            self.finished, self.status = True, status
            if self.timer:
                self.timer.cancel()
            if self.orphan:
                self.orphan.cancel()
            return True


def config_key(owner: uuid.UUID, seed_canonical: str, cfg: RunConfig) -> str:
    body = cfg.model_dump(mode="json", exclude={"preset_id"})
    body["seed_smiles"] = seed_canonical
    body["_owner"] = str(owner)  # replay caches are per user: nobody can probe what another user has run
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class RunManager:
    def __init__(
        self, settings: Settings, engine: Any, mock_events: list[dict], child_module: str = CHILD_MODULE
    ):
        """`engine` only decides live vs mock (the API process never runs it); live runs execute in a child
        process started from `child_module` (a module with the `runchild.serve` protocol; tests substitute one)."""
        self.s, self.engine, self.mock_events = settings, engine, mock_events
        self.child_module = child_module
        self.pool = SlotPool(settings.max_concurrent)
        self.runs: OrderedDict[str, Run] = OrderedDict()
        self.cache: OrderedDict[str, list[dict]] = OrderedDict()
        self._cache_owner: dict[str, uuid.UUID] = {}
        self.lock = threading.Lock()
        self._streams: dict[uuid.UUID, int] = {}

    # ------------------------------------------------------------ admission
    def _active(self) -> list[Run]:
        return [r for r in self.runs.values() if not r.finished and r.source != "replay"]

    def submit(
        self,
        owner: uuid.UUID,
        cfg: RunConfig,
        run_id: str,
        profile: ProfileBody | None = None,
        adapter: dict[str, str] | None = None,
    ) -> Run:
        key = config_key(owner, cfg.seed_smiles, cfg)
        cacheable = (
            profile is None and cfg.profile_id is None and adapter is None
        )  # a profile can change between runs and an adapter can be deleted: never replay
        with self.lock:
            cached = self.cache.get(key) if cacheable else None
            if cached is not None:
                self.cache.move_to_end(key)
                run = Run(run_id, owner, cfg, "replay", cache_key=key, pace=self.s.replay_delay)
                for i, ev in enumerate(cached):
                    ev = json.loads(json.dumps(ev))
                    ev.update(run_id=run_id, seq=i)
                    if ev["type"] == "run_start":
                        ev["payload"]["source"] = "replay"
                    run.events.append(ev)
                run.finished, run.status = True, "done"
                self._register(run)
                return run
            active = self._active()
            if sum(r.owner == owner for r in active) >= self.s.per_user_active:
                raise Rejected(
                    429,
                    f"You already have {self.s.per_user_active} runs in progress",
                    self._retry_after(active),
                )
            if len(active) >= self.s.max_concurrent + self.s.max_queue:
                raise Rejected(429, "Server is busy, try again shortly", self._retry_after(active))
            source = "mock" if self.engine is None else "live"
            run = Run(
                run_id,
                owner,
                cfg,
                source,
                cache_key=key if source == "live" and cacheable else None,
                profile=profile if source == "live" else None,
                adapter=adapter if source == "live" else None,
            )
            self._register(run)
        self.pool.submit(self._execute, run)
        self._arm_orphan(run)  # released again if the client never opens (or leaves) the event stream
        return run

    def _retry_after(self, active: list[Run]) -> int:
        now, best = time.monotonic(), self.s.run_timeout
        for r in active:
            if r.started:
                best = min(best, max(1.0, self.s.run_timeout - (now - r.started)))
        return int(best) + 1

    def _register(self, run: Run) -> None:
        self.runs[run.run_id] = run
        # per-user bound first (oldest finished run of that user), then the process-wide bound
        mine = [k for k, r in self.runs.items() if r.owner == run.owner and r.finished]
        while (
            len([1 for r in self.runs.values() if r.owner == run.owner]) > self.s.max_runs_per_user_kept
            and mine
        ):
            self.runs.pop(mine.pop(0), None)
        while len(self.runs) > self.s.max_runs_kept:
            oldest = next((k for k, r in self.runs.items() if r.finished), None)
            if oldest is None:
                break
            self.runs.pop(oldest)

    def get(self, run_id: str, owner: uuid.UUID) -> Run | None:
        """The caller's own run, else None. Another user's run is indistinguishable from a missing one."""
        run = self.runs.get(run_id)
        return run if run is not None and run.owner == owner else None

    def forget_user(self, owner: uuid.UUID) -> int:
        """Cancel and drop every run of one user, and their replay-cache entries (account deletion)."""
        with self.lock:
            mine = [k for k, r in self.runs.items() if r.owner == owner]
            dropped = [self.runs.pop(k) for k in mine]
            for k in [k for k, o in self._cache_owner.items() if o == owner]:
                self.cache.pop(k, None)
                self._cache_owner.pop(k, None)
            self._streams.pop(owner, None)
        for r in dropped:
            r.finish("cancelled", "Run cancelled", recoverable=True)
            self._kill(r)
            r.events.clear()
        return len(dropped)

    def cancel(self, run: Run) -> bool:
        done = run.finish("cancelled", "Run cancelled", recoverable=True)
        self._kill(run)
        return done

    @staticmethod
    def _kill(run: Run) -> None:
        """Stop the run's process now (the supervisor would also notice a finished run within a poll interval)."""
        child = run.child
        if child is not None:
            child.kill()

    def shutdown(self) -> None:
        """Cancel every run, kill every child process, stop the supervisors. No process outlives this call."""
        with self.lock:
            runs = list(self.runs.values())
        for r in runs:
            r.finish("cancelled", "Run cancelled", recoverable=True)
            self._kill(r)
        self.pool.shutdown(wait=True)
        for r in runs:
            self._kill(r)

    # ------------------------------------------------------------ streams
    def _arm_orphan(self, run: Run) -> None:
        """Cancel the run if no event stream reads it for `orphan_grace` seconds, so a closed tab or a dropped
        connection does not leave an engine run burning CPU for its whole time limit. A reconnecting EventSource
        (Last-Event-ID) re-attaches within the grace period and keeps the run."""
        if run.source == "replay" or self.s.orphan_grace <= 0:
            return
        with run.lock:
            if run.finished or run.streams > 0:
                return
            if run.orphan:
                run.orphan.cancel()
            run.orphan = threading.Timer(self.s.orphan_grace, self._orphan_expired, args=(run,))
            run.orphan.daemon = True
            run.orphan.start()

    def _orphan_expired(self, run: Run) -> None:
        with run.lock:
            listening = run.streams > 0
        if not listening and run.finish(
            "cancelled", "Run cancelled: no client was listening", recoverable=True
        ):
            self._kill(run)
            log.info("run %s cancelled: client gone", run.run_id[:6])

    def attach(self, run: Run) -> None:
        with run.lock:
            run.streams += 1
            if run.orphan:
                run.orphan.cancel()
                run.orphan = None

    def detach(self, run: Run) -> None:
        with run.lock:
            run.streams = max(0, run.streams - 1)
        self._arm_orphan(run)

    def open_stream(self, owner: uuid.UUID) -> bool:
        with self.lock:
            n = self._streams.get(owner, 0)
            if n >= self.s.per_user_streams:
                return False
            self._streams[owner] = n + 1
            return True

    def close_stream(self, owner: uuid.UUID) -> None:
        with self.lock:
            n = self._streams.get(owner, 0) - 1
            if n > 0:
                self._streams[owner] = n
            else:
                self._streams.pop(owner, None)

    # ------------------------------------------------------------ execution
    def _on_timeout(self, run: Run) -> None:
        if run.finish("error", f"Run exceeded the {self.s.run_timeout:g}s time limit", recoverable=True):
            log.warning("run %s timed out", run.run_id[:6])
        self._kill(run)  # a native call stuck inside the engine dies with its process

    def _execute(self, run: Run) -> None:
        if run.finished:  # cancelled while queued
            return
        run.status, run.started = "running", time.monotonic()
        run.timer = threading.Timer(self.s.run_timeout, self._on_timeout, args=(run,))
        run.timer.daemon = True
        run.timer.start()
        ended = "Engine ended without a result"
        try:
            if run.source == "mock":
                self._run_mock(run)
            else:
                ended = self._run_child(run) or ended
            with run.lock:
                kinds = [e["type"] for e in run.events]
            if "run_end" in kinds and "error" not in kinds:
                if run.finish("done") and run.cache_key:
                    self._remember(run)
            elif "error" in kinds:
                run.finish("error")
            else:
                run.finish("error", ended, recoverable=True)
        except Exception as exc:  # supervisor bugs must not kill the worker
            log.error("run %s failed: %s", run.run_id[:6], type(exc).__name__)
            run.finish("error", f"Engine failure: {type(exc).__name__}", recoverable=True)
        finally:
            if run.timer:
                run.timer.cancel()
            self._kill(run)
            log.info(
                "run %s source=%s status=%s events=%d secs=%.1f",
                run.run_id[:6],
                run.source,
                run.status,
                len(run.events),
                time.monotonic() - (run.started or run.created),
            )

    def _run_mock(self, run: Run) -> None:
        for ev in self._mock_iter(run):
            if not run.push(ev["type"], ev["payload"]):
                break  # timed out / cancelled
            if ev["type"] in ("run_end", "error"):
                break

    def _run_child(self, run: Run) -> str | None:
        """Run the GA in its own process and relay its events. Returns the message to use if the stream ends
        without a result (the process crashed, ran out of memory or was killed by its limits)."""
        spec = {
            "run_id": run.run_id,
            "config": run.config.model_dump(mode="json"),
            "profile": run.profile.model_dump(mode="json") if run.profile else None,
            "adapter": run.adapter,
            "mem_mb": self.s.child_mem_mb,
            "max_seconds": self.s.run_timeout + 5,
        }
        try:
            child = procutil.Child.spawn(self.child_module)
        except OSError:
            log.error("run %s: the run process could not be started", run.run_id[:6])
            return "The run could not be started, please try again"
        run.child = child
        if run.finished:  # cancelled while the process was starting
            child.kill()
            return None
        crashed = "The run stopped unexpectedly, please try again"
        try:
            child.send(json.dumps(spec).encode() + b"\n", 5.0)
            while not run.finished:
                try:
                    line = child.read_line(0.1)
                except procutil.ChildGone:
                    return crashed
                except procutil.ChildMisbehaved:
                    return "The run produced an invalid result"
                if line is None:
                    continue
                try:
                    msg = json.loads(line)
                    if "fail" in msg:
                        return (
                            "The run needed more memory than allowed"
                            if msg["fail"] == "memory"
                            else f"Engine failure: {str(msg['fail'])[:40]}"
                        )
                    kind, payload = msg["type"], msg["payload"]
                    if kind not in EVENT_TYPES or not isinstance(payload, dict):
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    return "The run produced an invalid result"
                if len(run.events) >= self.s.max_run_events:
                    return "The run produced too many results"
                if not run.push(kind, payload) or kind in ("run_end", "error"):
                    break
        except (procutil.ChildGone, TimeoutError):
            return crashed
        finally:
            child.kill()
            run.child = None
        return None

    def _mock_iter(self, run: Run):
        for ev in self.mock_events:
            if run.finished:
                return
            time.sleep(self.s.mock_delay)
            ev = json.loads(json.dumps(ev))
            if ev["type"] == "run_start":
                ev["payload"]["source"] = "mock"
            yield ev

    def _remember(self, run: Run) -> None:
        key = run.cache_key
        if key is None:
            return
        with self.lock:
            self.cache[key] = list(run.events)
            self._cache_owner[key] = run.owner
            while len(self.cache) > self.s.cache_size:
                old, _ = self.cache.popitem(last=False)
                self._cache_owner.pop(old, None)
