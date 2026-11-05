"""Run management and limits for the Lead Studio service.

Every run belongs to one Metis user (`owner`); every lookup takes the caller's user id and answers "unknown" for
anyone else's run, so user B can never read, stream or cancel user A's run. State is in memory of THIS process
(the service is single-process by design; see docs/OPERATIONS.md). Rate limiting of the HTTP routes is done by the
shared database limiter in `backend.accounts.deps.rate_limit`, not here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from leadstudio.schema import RunConfig

log = logging.getLogger("leadstudio.server")


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
    run_timeout: float = 90.0  # wall-clock seconds from execution start
    max_generations: int = 15
    max_population: int = 40
    max_heavy_atoms: int = 60
    max_heavy_ops: int = 6  # validate / preview / applicable computations running at once in this process
    runs_per_minute: int = 6  # per user, enforced by the shared database limiter
    validates_per_minute: int = 30
    heartbeat: float = 15.0
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
            max_generations=_env("STUDIO_MAX_GENERATIONS", d.max_generations, int),
            max_population=_env("STUDIO_MAX_POPULATION", d.max_population, int),
            runs_per_minute=_env("STUDIO_RUNS_PER_MIN", d.runs_per_minute, int),
            validates_per_minute=_env("STUDIO_VALIDATES_PER_MIN", d.validates_per_minute, int),
            heartbeat=_env("STUDIO_HEARTBEAT", d.heartbeat),
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


class HeavyGate:
    """Bounds how many CPU-heavy request handlers (validate, preview, applicable) run at once in this process, so a
    burst cannot starve the thread pool that serves every other Metis route. Non-blocking: busy -> 429."""

    def __init__(self, limit: int):
        self._sem = threading.BoundedSemaphore(max(1, limit))

    def __enter__(self) -> HeavyGate:
        if not self._sem.acquire(blocking=False):
            raise Rejected(429, "Lead Studio is busy, try again in a moment", 2)
        return self

    def __exit__(self, *exc: object) -> None:
        self._sem.release()


@dataclass
class Run:
    run_id: str
    owner: uuid.UUID
    config: RunConfig
    source: str  # live | mock | replay
    cache_key: str | None = None
    pace: float = 0.0  # delay applied by the SSE writer (replays only)
    scorer_wrap: Callable[[Any], Any] | None = (
        None  # per-run scorer wrapper (a saved profile), live runs only
    )
    events: list[dict] = field(default_factory=list)
    finished: bool = False
    status: str = "queued"  # queued | running | done | error | cancelled
    started: float | None = None
    created: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)
    timer: threading.Timer | None = None

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
            return True


def config_key(owner: uuid.UUID, seed_canonical: str, cfg: RunConfig) -> str:
    body = cfg.model_dump(mode="json", exclude={"preset_id"})
    body["seed_smiles"] = seed_canonical
    body["_owner"] = str(owner)  # replay caches are per user: nobody can probe what another user has run
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class RunManager:
    def __init__(self, settings: Settings, engine: Any, mock_events: list[dict]):
        self.s, self.engine, self.mock_events = settings, engine, mock_events
        self.pool = ThreadPoolExecutor(max_workers=settings.max_concurrent, thread_name_prefix="lead-run")
        self.runs: OrderedDict[str, Run] = OrderedDict()
        self.cache: OrderedDict[str, list[dict]] = OrderedDict()
        self.lock = threading.Lock()
        self._streams: dict[uuid.UUID, int] = {}

    # ------------------------------------------------------------ admission
    def _active(self) -> list[Run]:
        return [r for r in self.runs.values() if not r.finished and r.source != "replay"]

    def submit(
        self, owner: uuid.UUID, cfg: RunConfig, run_id: str, scorer_wrap: Callable[[Any], Any] | None = None
    ) -> Run:
        key = config_key(owner, cfg.seed_smiles, cfg)
        cacheable = (
            scorer_wrap is None and cfg.profile_id is None
        )  # a profile can change between runs: never replay
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
                scorer_wrap=scorer_wrap if source == "live" else None,
            )
            self._register(run)
        self.pool.submit(self._execute, run)
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

    def cancel(self, run: Run) -> bool:
        return run.finish("cancelled", "Run cancelled", recoverable=True)

    # ------------------------------------------------------------ streams
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

    def _engine_for(self, run: Run) -> Any:
        if run.scorer_wrap is not None and hasattr(self.engine, "with_scorer"):
            return self.engine.with_scorer(run.scorer_wrap)
        return self.engine

    def _execute(self, run: Run) -> None:
        if run.finished:  # cancelled while queued
            return
        run.status, run.started = "running", time.monotonic()
        run.timer = threading.Timer(self.s.run_timeout, self._on_timeout, args=(run,))
        run.timer.daemon = True
        run.timer.start()
        try:
            gen = (
                self._mock_iter(run)
                if run.source == "mock"
                else self._engine_for(run).run(run.run_id, run.config)
            )
            try:
                for ev in gen:
                    d = ev if isinstance(ev, dict) else ev.model_dump(mode="json")
                    if not run.push(d["type"], d["payload"]):
                        break  # timed out / cancelled
                    if d["type"] in ("run_end", "error"):
                        break
            finally:
                close = getattr(gen, "close", None)
                if close:
                    close()
            with run.lock:
                kinds = [e["type"] for e in run.events]
            if "run_end" in kinds and "error" not in kinds:
                if run.finish("done") and run.cache_key:
                    self._remember(run)
            elif "error" in kinds:
                run.finish("error")
            else:
                run.finish("error", "Engine ended without a result", recoverable=True)
        except Exception as exc:  # engine bugs must not kill the worker
            log.error("run %s failed: %s", run.run_id[:6], type(exc).__name__)
            run.finish("error", f"Engine failure: {type(exc).__name__}", recoverable=True)
        finally:
            if run.timer:
                run.timer.cancel()
            log.info(
                "run %s source=%s status=%s events=%d secs=%.1f",
                run.run_id[:6],
                run.source,
                run.status,
                len(run.events),
                time.monotonic() - (run.started or run.created),
            )

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
        with self.lock:
            self.cache[run.cache_key] = list(run.events)
            while len(self.cache) > self.s.cache_size:
                self.cache.popitem(last=False)
