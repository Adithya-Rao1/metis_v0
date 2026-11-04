"""Ranking engine, spec section 7, implemented exactly.

    R_j = (1 - lam) S_j + lam C_j
    S_j = sum_i w_i d_ij / sum_i w_i        (property score)
    C_j = sum_i w_i c_ij / sum_i w_i        (confidence score)

d_ij: linear min-max desirability on [target_min, target_max] (defaults from the model's training range),
      flipped for lower-is-better, clamped to [0, 1].
c_ij: regression  1 - width / (2 * median_calibration_width), floored at 0.
      classification  1 - binary entropy (bits) of the calibrated probability.
Hard limits apply to the point estimate (or the unfavorable interval bound when requested). Limit failers stay
in the output, rank below every passer, and are flagged. Both the blended and the property-only (lam = 0)
rank are returned with the per-property stacked breakdown. There is no verdict field.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel

from backend.schemas.contracts import HardLimit, Prediction, RankingConfig


class RankingError(ValueError):
    """Inputs are insufficient to rank (missing prediction, range or calibration width)."""


@dataclass(frozen=True)
class EndpointInfo:
    default_range: tuple[float, float]
    median_width: Optional[float] = None  # median calibration interval width (regression only)


class PropertyBreakdown(BaseModel):
    endpoint: str
    weight_share: float  # w_i / sum w
    desirability: float
    confidence: float
    property_contribution: float  # w_i d_ij / sum w
    confidence_contribution: float  # w_i c_ij / sum w
    blended_property_part: float  # (1 - lam) * property_contribution
    blended_confidence_part: float  # lam * confidence_contribution


class LimitFailure(BaseModel):
    endpoint: str
    note: str


class RankedCompound(BaseModel):
    compound_id: str
    property_score: float
    confidence_score: float
    blended_score: float
    blended_rank: int
    property_rank: int
    passes_limits: bool
    limit_failures: list[LimitFailure]
    breakdown: list[PropertyBreakdown]


def desirability(value: float, lo: float, hi: float, higher_is_better: bool) -> float:
    if not hi > lo:
        raise RankingError("desirability range must have min below max")
    d = (value - lo) / (hi - lo) if higher_is_better else (hi - value) / (hi - lo)
    return min(1.0, max(0.0, d))


def binary_entropy(p: float) -> float:
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


def confidence(pred: Prediction, median_width: Optional[float]) -> float:
    if pred.kind == "classification":
        if pred.probability is None:
            raise RankingError("classification prediction without probability")
        return 1.0 - binary_entropy(pred.probability)
    if median_width is None or not median_width > 0:
        raise RankingError(f"no median calibration width for endpoint {pred.endpoint}")
    if pred.interval_low is None or pred.interval_high is None:
        raise RankingError("regression prediction without interval")
    width = pred.interval_high - pred.interval_low
    return max(0.0, 1.0 - width / (2.0 * median_width))


def point_value(pred: Prediction) -> float:
    return pred.probability if pred.kind == "classification" and pred.probability is not None else pred.value


def _fmt(x: float) -> str:
    return f"{x:g}"


def check_limit(pred: Prediction, limit: HardLimit) -> Optional[str]:
    """Return a 'limit: ...' note when violated, else None."""
    if pred.kind == "regression" and limit.use_unfavorable_bound:
        lo_v, hi_v = pred.interval_low, pred.interval_high
        basis = "unfavorable interval bound"
    else:
        lo_v = hi_v = point_value(pred)
        basis = "point estimate"
    if lo_v is None or hi_v is None:
        raise RankingError("prediction without usable value")
    if limit.min is not None and lo_v < limit.min:
        return f"limit: >= {_fmt(limit.min)} ({basis} {_fmt(lo_v)})"
    if limit.max is not None and hi_v > limit.max:
        return f"limit: <= {_fmt(limit.max)} ({basis} {_fmt(hi_v)})"
    return None


def _competition_ranks(scores: list[float], groups: list[int]) -> list[int]:
    """Rank by (group asc, score desc). Equal scores within a group share the lowest rank (1224)."""
    order = sorted(range(len(scores)), key=lambda i: (groups[i], -scores[i]))
    ranks = [0] * len(scores)
    for pos, i in enumerate(order):
        if pos > 0:
            prev = order[pos - 1]
            if groups[prev] == groups[i] and scores[prev] == scores[i]:
                ranks[i] = ranks[prev]
                continue
        ranks[i] = pos + 1
    return ranks


def rank_compounds(
    predictions: dict[str, dict[str, Prediction]],
    config: RankingConfig,
    info: dict[str, EndpointInfo],
) -> list[RankedCompound]:
    """predictions: compound_id -> endpoint -> Prediction. Returned in input order (ranks are fields)."""
    w_total = sum(w.weight for w in config.weights)
    if not w_total > 0:
        raise RankingError("at least one weight must be positive")
    lam = config.lam
    rows: list[RankedCompound] = []
    for cid, preds in predictions.items():
        need = {w.endpoint for w in config.weights} | {lim.endpoint for lim in config.limits}
        missing = sorted(e for e in need if e not in preds)
        if missing:
            raise RankingError(f"missing predictions for endpoints: {', '.join(missing)}")
        s = c = 0.0
        parts: list[PropertyBreakdown] = []
        for w in config.weights:
            pred = preds[w.endpoint]
            ei = info.get(w.endpoint)
            if w.target_min is not None and w.target_max is not None:
                lo, hi = w.target_min, w.target_max
            elif ei is not None:
                lo, hi = ei.default_range
            else:
                raise RankingError(f"no desirability range for endpoint {w.endpoint}")
            d = desirability(point_value(pred), lo, hi, w.higher_is_better)
            cc = confidence(pred, ei.median_width if ei else None)
            share = w.weight / w_total
            s += share * d
            c += share * cc
            parts.append(
                PropertyBreakdown(
                    endpoint=w.endpoint,
                    weight_share=share,
                    desirability=d,
                    confidence=cc,
                    property_contribution=share * d,
                    confidence_contribution=share * cc,
                    blended_property_part=(1 - lam) * share * d,
                    blended_confidence_part=lam * share * cc,
                )
            )
        fails = [
            LimitFailure(endpoint=lim.endpoint, note=note)
            for lim in config.limits
            if (note := check_limit(preds[lim.endpoint], lim)) is not None
        ]
        rows.append(
            RankedCompound(
                compound_id=cid,
                property_score=s,
                confidence_score=c,
                blended_score=(1 - lam) * s + lam * c,
                blended_rank=0,
                property_rank=0,
                passes_limits=not fails,
                limit_failures=fails,
                breakdown=parts,
            )
        )
    groups = [0 if r.passes_limits else 1 for r in rows]
    blended = _competition_ranks([r.blended_score for r in rows], groups)
    prop = _competition_ranks([r.property_score for r in rows], groups)
    for r, b, p in zip(rows, blended, prop):
        r.blended_rank, r.property_rank = b, p
    return rows
