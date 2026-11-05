"""Per-app Lead Studio state: engine, services, run manager, pose manager. Built lazily on first use.

Tests (and `scripts/demo.py`) can pre-build one with `build_state(...)` and assign it to `app.state.lead`.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request

from leadstudio.server import stages
from leadstudio.server.core import HeavyGate, RunManager, Settings
from leadstudio.server.manual import ManualService
from leadstudio.server.poses import PoseManager
from leadstudio.wiring import describe

log = logging.getLogger("leadstudio.server")

ROOT = Path(__file__).resolve().parents[1]
MOCK = ROOT / "mock" / "mock_run.json"
PRESETS = ROOT / "mock" / "presets.json"

# Conventional entry points for the engine / scorer factories in leadstudio.wiring.
_FACTORIES = {"engine": "build_engine", "scorer": "build_scorer"}
_build_lock = threading.Lock()


@dataclass
class LeadState:
    settings: Settings
    engine: Any  # StageEngine around the real Engine, or None in mock mode
    scorer: Any
    svc: stages.Services
    mgr: RunManager
    poses: PoseManager
    manual: ManualService
    heavy: HeavyGate
    details: dict[str, Any]
    components: dict[str, Any]
    presets: list[Any]

    @property
    def source(self) -> str:
        return "live" if self.engine is not None else "mock"


def _load_component(name: str) -> Any | None:
    """Build the engine / scorer, or None if it cannot be built (the service then serves the mock replay)."""
    try:
        from leadstudio import wiring

        fn = getattr(wiring, _FACTORIES[name])
        if inspect.isfunction(fn):
            return fn()
    except Exception as exc:
        log.warning("component %s unavailable: %s", name, type(exc).__name__)
    return None


def _module_present(modname: str) -> bool:
    try:
        return importlib.util.find_spec(modname) is not None
    except Exception:
        return False


def _load_presets() -> list[Any]:
    try:
        return json.loads(PRESETS.read_text()) if PRESETS.exists() else []
    except (OSError, ValueError):
        log.warning("presets.json unreadable")
        return []


def build_state(
    engine: Any = None,
    scorer: Any = None,
    settings: Settings | None = None,
    services: stages.Services | None = None,
    pose_client: Any = None,
    allowed_dock_url: str | None = None,
) -> LeadState:
    s = settings or Settings.from_env()
    if s.force_mock:
        engine, scorer = None, None
    else:
        engine = engine or _load_component("engine")
        scorer = scorer or _load_component("scorer")
    svc = services or stages.Services()
    if pose_client is not None:
        svc.pose_client = pose_client
    live = engine is not None
    details = describe(engine)  # describe the real engine, before the stage wrapper hides its parts
    if live:
        engine = stages.StageEngine(engine, svc)  # routes stage=binding to the binding engine
    mock_events = json.loads(MOCK.read_text()) if MOCK.exists() else []
    components = {
        "engine": engine is not None,
        "scorer": scorer is not None,
        "geometry": engine is not None and _module_present("leadstudio.engine.geometry"),
        "registry": details["registry_ops"] > 0,
        "explain": details["explainer"] is not None,
        "embed": details["embedder"] is not None,
        "policy": details["policy"] is not None,
        "mock_run": bool(mock_events),
        **({} if s.force_mock else svc.health()),
    }
    return LeadState(
        settings=s,
        engine=engine,
        scorer=scorer,
        svc=svc,
        mgr=RunManager(s, engine, mock_events),
        poses=PoseManager(
            svc,
            allowed_url=allowed_dock_url,
            poll=_envf("DYNAMICBIND_POLL", 2.0),
            timeout=_envf("DYNAMICBIND_TIMEOUT", 900.0),
        ),
        manual=ManualService(svc, scorer, s),
        heavy=HeavyGate(s.max_heavy_ops),
        details=details,
        components=components,
        presets=_load_presets(),
    )


def _envf(name: str, default: float) -> float:
    import os

    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def get_state(request: Request) -> LeadState:
    st = getattr(request.app.state, "lead", None)
    if st is None:
        with _build_lock:
            st = getattr(request.app.state, "lead", None)
            if st is None:
                from backend.core.settings import get_settings

                st = request.app.state.lead = build_state(allowed_dock_url=get_settings().lead_dock_url)
    return st
