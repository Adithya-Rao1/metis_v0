"""Integrator-owned: assemble the real components into the Engine the server runs.

Everything is constructed lazily and falls back honestly: a missing policy checkpoint
means UniformPolicy (the UI/health report it), a missing ChemBERTa means Morgan.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("leadstudio.wiring")


class FrozenPolicy:
    """Read-only view of a policy. The engine calls `update()` after every generation; on a shared multi-user service a
    learning policy would carry one user's edits into another user's runs, so updates are dropped here."""

    def __init__(self, inner):
        self._inner = inner
        self.checkpoint_loaded = getattr(inner, "checkpoint_loaded", None)

    @classmethod
    def wrap(cls, policy):
        return cls(policy)

    def rank(self, smiles, candidates):
        return self._inner.rank(smiles, candidates)

    def update(self, transitions):
        return None


def build_scorer():
    from leadstudio.scoring import RDKitScorer

    return RDKitScorer()


def build_engine(scorer=None):
    # Heavy imports (rdkit, deap) are deferred so importing the server stays fast.
    from leadstudio.embed import get_embedder
    from leadstudio.engine import Engine
    from leadstudio.explain.explainer import RDKitExplainer
    from leadstudio.policy.policy import load_policy
    from leadstudio.registry import get_registry

    registry = get_registry()
    policy = FrozenPolicy.wrap(load_policy([op.id for op in registry.all()]))
    log.info(
        "policy=%s embedder=%s ops=%d",
        type(policy._inner).__name__,
        get_embedder().method,
        len(registry.all()),
    )
    return Engine(registry, scorer or build_scorer(), RDKitExplainer(), get_embedder(), policy)


def describe(engine=None) -> dict:
    """Truthful description of what the server is running (exposed by /lead/api/health).

    `engine` None means the mock replay is serving. Attributes missing on an injected
    stand-in engine are reported as absent, never guessed.
    """
    if engine is None:
        return {
            "engine_source": "mock",
            "registry_ops": 0,
            "scorer": None,
            "explainer": None,
            "embedder": None,
            "policy": None,
            "demo_safe": False,
        }
    registry, policy = getattr(engine, "registry", None), getattr(engine, "policy", None)
    policy = getattr(policy, "_inner", policy)  # see FrozenPolicy
    ckpt = getattr(policy, "checkpoint_loaded", None)
    if policy is None:
        pol = None
    elif ckpt:
        pol = {"kind": "dqn", "checkpoint": os.path.basename(ckpt)}
    else:
        pol = {"kind": "uniform", "checkpoint": None, "note": type(policy).__name__ + " fallback"}
    return {
        "engine_source": "live",
        "registry_ops": len(registry.all()) if registry is not None else 0,
        "scorer": type(engine.scorer).__name__ if getattr(engine, "scorer", None) else None,
        "explainer": type(engine.explainer).__name__ if getattr(engine, "explainer", None) else None,
        "embedder": getattr(getattr(engine, "embedder", None), "method", None),
        "policy": pol,
        # No demo-safe edit filtering exists in the registry on this branch.
        "demo_safe": bool(getattr(registry, "demo_safe", False)),
    }
