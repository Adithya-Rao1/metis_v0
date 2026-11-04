"""Frozen API contracts (Wave 0). Only the integrator changes these; see docs/decisions/.

Spec refs: 3 (10-compound cap), 5 (uncertainty fields), 6.6 (ensemble sizes), 7 (ranking).
"""
from __future__ import annotations

import re
from typing import Annotated, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

ENSEMBLE_SIZES = (3, 5, 10, 20)
MAX_COMPOUNDS = 10
TIER_MAX_N = {"free": 10, "edu": 20, "paid": 20}
DEFAULT_N = 5
DEFAULT_LAMBDA = 0.25


SMILES_RE = re.compile(r"^[\x21-\x7e]{1,2000}$")  # printable ASCII, no whitespace/control/zero-width
Endpoint = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,64}$")]
MAX_ENDPOINTS = 8


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class UncertaintyBreakdown(Strict):
    """Shares of predicted uncertainty by source; sums to 1 (spec 6.3)."""
    ensemble_disagreement: float = Field(ge=0, le=1)
    similarity_to_training: float = Field(ge=0, le=1)
    assay_noise: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sums_to_one(self):
        total = self.ensemble_disagreement + self.similarity_to_training + self.assay_noise
        if abs(total - 1.0) > 1e-3:
            raise ValueError("breakdown shares must sum to 1")
        return self


class Prediction(Strict):
    """Every prediction carries value, interval or calibrated probability, method note, N,
    coverage and sample size. No verdict field exists by design (spec 5)."""
    endpoint: Endpoint
    kind: Literal["regression", "classification"]
    model_version_id: UUID
    ensemble_n: Literal[3, 5, 10, 20]
    value: float
    interval_low: Optional[float] = None
    interval_high: Optional[float] = None
    probability: Optional[float] = Field(default=None, ge=0, le=1)
    coverage: float = Field(gt=0, lt=1)
    sample_size: int = Field(gt=0, le=10**9)
    method_note: str = Field(min_length=1, max_length=2000)
    breakdown: UncertaintyBreakdown

    @model_validator(mode="after")
    def _kind_fields(self):
        if self.kind == "regression":
            if self.interval_low is None or self.interval_high is None or self.interval_low > self.interval_high:
                raise ValueError("regression requires an ordered interval")
            if not self.interval_low <= self.value <= self.interval_high:
                raise ValueError("value must lie inside its interval")
        else:
            if self.probability is None:
                raise ValueError("classification requires a calibrated probability")
            if self.interval_low is not None or self.interval_high is not None:
                raise ValueError("classification carries a probability, not an interval")
        if not self.method_note.strip():
            raise ValueError("method note must not be blank")
        return self


class CompoundSubmission(Strict):
    smiles: list[str] = Field(min_length=1, max_length=MAX_COMPOUNDS)
    ensemble_n: Literal[3, 5, 10, 20] = DEFAULT_N
    endpoints: list[Endpoint] = Field(min_length=1, max_length=MAX_ENDPOINTS)

    @model_validator(mode="after")
    def _hygiene(self):
        if any(not SMILES_RE.match(x) for x in self.smiles):
            raise ValueError("SMILES must be 1-2000 printable ASCII characters with no whitespace")
        if len(set(self.smiles)) != len(self.smiles):
            raise ValueError("duplicate SMILES")
        if len(set(self.endpoints)) != len(self.endpoints):
            raise ValueError("duplicate endpoints")
        return self


class PropertyWeight(Strict):
    endpoint: Endpoint
    weight: float = Field(ge=0)
    higher_is_better: bool
    # Desirability range: value <= target_min (or >= for lower-is-better) scores 0, the other end scores 1, linear between.
    # None means "use the model's default range from its training data"; users may edit it (decision 0002).
    target_min: Optional[float] = None
    target_max: Optional[float] = None

    @model_validator(mode="after")
    def _range_ok(self):
        if (self.target_min is None) != (self.target_max is None):
            raise ValueError("set both target_min and target_max, or neither")
        if self.target_min is not None and self.target_min >= self.target_max:
            raise ValueError("target_min must be below target_max")
        return self


class HardLimit(Strict):
    endpoint: Endpoint
    min: Optional[float] = None
    max: Optional[float] = None
    use_unfavorable_bound: bool = False

    @model_validator(mode="after")
    def _ordered(self):
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("limit min must not exceed max")
        return self


class RankingConfig(Strict):
    weights: list[PropertyWeight] = Field(min_length=1, max_length=MAX_ENDPOINTS)
    limits: list[HardLimit] = Field(default=[], max_length=MAX_ENDPOINTS)
    lam: float = Field(default=DEFAULT_LAMBDA, ge=0, le=1)  # 0 = property only, 1 = confidence only

    @model_validator(mode="after")
    def _some_weight(self):
        if sum(w.weight for w in self.weights) <= 0:
            raise ValueError("at least one weight must be positive")
        if len({w.endpoint for w in self.weights}) != len(self.weights) or len({l.endpoint for l in self.limits}) != len(self.limits):
            raise ValueError("duplicate endpoints")
        return self


def allowed_ensemble_sizes(tier: str) -> tuple[int, ...]:
    cap = TIER_MAX_N[tier]
    return tuple(n for n in ENSEMBLE_SIZES if n <= cap)
