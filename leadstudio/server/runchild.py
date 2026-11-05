"""Child-process side of one Lead Studio run: `python -m leadstudio.server.runchild`.

Started by `RunManager` through `procutil.Child.spawn` with a minimal environment. The first line on stdin is the
run specification {run_id, config, profile, mem_mb, max_seconds}; the events go back as JSON lines on file
descriptor 3. The process builds its own engine (nothing is inherited from the API process), runs the GA and exits.
It never logs and never prints SMILES; stdout and stderr are /dev/null.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import Any

from leadstudio.server import procutil


def default_builder() -> Any:
    from leadstudio.wiring import build_engine

    return build_engine()


class _AdapterPolicy:
    """The user's adapted policy for one run: frozen (no learning from the run) and with a note the run reports."""

    def __init__(self, inner: Any, note: str):
        self._inner, self.note = inner, note
        self.checkpoint_loaded = getattr(inner, "checkpoint_loaded", None)

    def rank(self, smiles: Any, candidates: Any) -> Any:
        return self._inner.rank(smiles, candidates)

    def update(self, transitions: Any) -> None:
        return None


def _adapter_policy(pin: dict[str, str]) -> Any:
    """Load the pinned adapter (sha256 checked on the bytes that are loaded, `weights_only=True`). If it cannot be
    used the run goes on with the base policy and says so in its policy notes."""
    from leadstudio.adapt.policy_factory import load_adapted_policy

    pol = load_adapted_policy(pin["adapter_id"], pin["owner"], sha256=pin["sha256"])
    info = getattr(pol, "adapter_info", {}) or {}
    if info.get("used"):
        return _AdapterPolicy(pol, "your adapted policy is in use")
    return _AdapterPolicy(
        pol, "your adapted policy was not used (" + str(info.get("reason", "unknown"))[:60] + ")"
    )


def _send(obj: dict) -> None:
    procutil.write_event_line(json.dumps(obj, separators=(",", ":")).encode())


def assemble(engine: Any, spec: dict) -> Any:
    """The run's own engine: the shared one with the caller's saved profile (scoring and edit allow-list) and adapted
    policy applied to a copy. Nothing shared is mutated, and each part is applied only if the spec carries it."""
    if spec.get("profile") is not None:
        from leadstudio.profile_scorer import AllowlistRegistry, ProfileScorer
        from leadstudio.profiles import ProfileBody

        prof = ProfileBody.model_validate(spec["profile"])
        engine = engine.with_scorer(lambda base: ProfileScorer(base, prof))
        if prof.edit_allowlist:  # the GA proposes only these edits
            engine = engine.with_registry(lambda reg: AllowlistRegistry(reg, prof.edit_allowlist))
    if spec.get("adapter") is not None:
        engine = engine.with_policy(_adapter_policy(spec["adapter"]))
    return engine


def serve(builder: Callable[[], Any] | None = None) -> None:
    """Run one specification to completion. `builder` returns the engine (tests substitute stand-ins)."""
    line = procutil.read_stdin_line()
    if line is None:
        os._exit(70)
    spec = json.loads(line)
    procutil.disable_core_dumps()
    procutil.limit_address_space(int(spec.get("mem_mb") or 0))
    procutil.hard_deadline(float(spec.get("max_seconds") or 0))
    procutil.exit_when_parent_gone()
    code = 0
    try:
        from leadstudio.schema import RunConfig
        from leadstudio.server.stages import Services, StageEngine

        engine = assemble(StageEngine((builder or default_builder)(), Services()), spec)
        cfg = RunConfig.model_validate(spec["config"])
        for ev in engine.run(spec["run_id"], cfg):
            d = ev if isinstance(ev, dict) else ev.model_dump(mode="json")
            _send({"type": d["type"], "payload": d["payload"]})
            if d["type"] in ("run_end", "error"):
                break
    except MemoryError:
        _send({"fail": "memory"})
        code = 3
    except BrokenPipeError:
        code = 70
    except Exception as exc:
        _send({"fail": type(exc).__name__})
        code = 2
    sys.stdout = sys.stderr = None
    os._exit(code)


if __name__ == "__main__":
    serve()
