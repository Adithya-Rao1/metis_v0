"""Uncertainty decomposition into shares: ensemble disagreement / distance from training / assay noise.

Working definition (relative shares, not a causal partition; state this wherever shown):
  e = variance of member predictions (ensemble disagreement)
  a = assay noise variance, if a noise SD is known for the endpoint; otherwise UNKNOWN and share = 0
  s = expected squared error implied by nearest-neighbour similarity (from the calibration set)
  d = max(s - e - a, 0)   the part of the expected error not already explained by e and a
  shares = (e, d, a) / (e + d + a)
Ranges: bootstrap over ensemble members (with replacement), seeded, 5th-95th percentile.
With small N the ranges are wide on purpose.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from backend.schemas.contracts import UncertaintyBreakdown

NOTE = ("Shares are relative contributions: ensemble disagreement = variance across members; distance from "
        "training = expected squared error at this nearest-neighbour Tanimoto similarity beyond what disagreement "
        "and assay noise explain; assay noise is {noise}. Ranges are 5th-95th percentile over member resamples.")


@dataclass
class Decomposition:
    ensemble_disagreement: float
    similarity_to_training: float
    assay_noise: float
    ranges: dict[str, tuple[float, float]]
    assay_noise_known: bool
    degenerate: bool = False
    note: str = field(default="")

    def to_breakdown(self) -> UncertaintyBreakdown:
        # Renormalise so the three floats sum to 1 within the contract tolerance after rounding.
        v = np.array([self.ensemble_disagreement, self.similarity_to_training, self.assay_noise])
        v = v / v.sum()
        return UncertaintyBreakdown(ensemble_disagreement=float(v[0]), similarity_to_training=float(v[1]),
                                    assay_noise=float(v[2]))


def _shares(member_preds: np.ndarray, sim_sigma: float, noise_sd: float | None) -> tuple[np.ndarray, bool]:
    e = float(np.var(member_preds, ddof=1)) if len(member_preds) > 1 else 0.0
    a = float(noise_sd) ** 2 if noise_sd is not None else 0.0
    d = max(float(sim_sigma) ** 2 - e - a, 0.0)
    tot = e + d + a
    if tot <= 1e-18:
        return np.array([0.0, 1.0, 0.0]), True
    return np.array([e, d, a]) / tot, False


def decompose(member_preds, sim_sigma: float, noise_sd: float | None = None, n_resamples: int = 200,
              seed: int = 0, lo_q: float = 5, hi_q: float = 95) -> Decomposition:
    """member_preds: predictions from the N members for one compound. sim_sigma: expected abs error from
    the similarity model (SimilarityErrorModel.predict). noise_sd None => assay noise unknown (share 0)."""
    mp = np.asarray(member_preds, float)
    if mp.ndim != 1 or len(mp) < 2:
        raise ValueError("need predictions from at least 2 members")
    point, degenerate = _shares(mp, sim_sigma, noise_sd)
    rng = np.random.default_rng(seed)
    boots = np.array([_shares(mp[rng.integers(0, len(mp), len(mp))], sim_sigma, noise_sd)[0] for _ in range(n_resamples)])
    lo, hi = np.percentile(boots, lo_q, axis=0), np.percentile(boots, hi_q, axis=0)
    names = ("ensemble_disagreement", "similarity_to_training", "assay_noise")
    ranges = {n: (float(min(lo[i], point[i])), float(max(hi[i], point[i]))) for i, n in enumerate(names)}
    known = noise_sd is not None
    return Decomposition(float(point[0]), float(point[1]), float(point[2]), ranges, known, degenerate,
                         NOTE.format(noise="known" if known else "unknown for this endpoint (shown as 0, not estimated)"))
