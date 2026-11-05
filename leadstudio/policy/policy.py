"""Edit-proposal prior for the GA (implements `leadstudio.contracts.Policy`).

`UniformPolicy` is always available. The learned DQN-style prior lives in `leadstudio.policy.dqn` and needs torch,
which is NOT a runtime dependency of the public service: `load_policy` imports it only when a trained checkpoint file
exists, and falls back to `UniformPolicy` if torch or the checkpoint is unusable. No checkpoint ships with this repo, so
the service runs the uniform policy (the health endpoint says so).
"""

from __future__ import annotations

import os
from collections.abc import Sequence

CKPT_DIR = os.path.join(os.path.dirname(__file__), "checkpoints")
DEFAULT_CKPT = os.path.join(CKPT_DIR, "policy.pt")


class UniformPolicy:
    """Always-available fallback: every candidate equally preferred."""

    def rank(self, smiles: str, candidates: Sequence[tuple]) -> list[float]:
        return [0.0] * len(candidates)

    def update(self, transitions: Sequence[tuple]) -> float | None:
        return None


def load_policy(op_ids: Sequence[str], checkpoint: str = "auto") -> object:
    """DQNPolicy if torch + a checkpoint are usable, else UniformPolicy."""
    path = DEFAULT_CKPT if checkpoint == "auto" else checkpoint
    if not path or not os.path.exists(path):
        return UniformPolicy()  # nothing to load: do not even import torch
    try:
        from leadstudio.policy.dqn import DQNPolicy

        p = DQNPolicy(op_ids, checkpoint=checkpoint)
        return p if p.checkpoint_loaded else UniformPolicy()
    except Exception:
        return UniformPolicy()
