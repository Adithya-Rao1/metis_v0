"""Frozen API contracts (Wave 0). Only the integrator changes these; see docs/decisions/.

Spec refs: 3 (10-compound cap), 5 (uncertainty fields), 6.6 (ensemble sizes), 7 (ranking).
"""
from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

ENSEMBLE_SIZES = (3, 5, 10, 20)
MAX_COMPOUNDS = 10
TIER_MAX_N = {"free": 10, "edu": 20, "paid": 20}
DEFAULT_N = 5
DEFAULT_LAMBDA = 0.25


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UncertaintyBreakdown(Strict):
    """Shares of predicted uncertainty by source; sums to 1 (spec 6.3)."""
    ensemble_disagreement: float = Field(ge=0, le=1)
    similarity_to_training: float = Field(ge=0, le=1)
    assay_noise: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sums_to_one(self):
        total = self.ensemble_disagreement + self.similarity_to_training + self.assay_noise
        if abs(total - 1.0) > 1e-6:
            raise ValueError("breakdown shares must sum to 1")
        return self


class Prediction(Strict):
    """Every prediction carries value, interval or calibrated probability, method note, N,
    coverage and sample size. No verdict field exists by design (spec 5)."""
    endpoint: str
    kind: Literal["regression", "classification"]
    model_version_id: UUID
    ensemble_n: Literal[3, 5, 10, 20]
    value: float
    interval_low: Optional[float] = None
    interval_high: Optional[float] = None
    probability: Optional[float] = Field(default=None, ge=0, le=1)
    coverage: float = Field(gt=0, lt=1)
    sample_size: int = Field(gt=0)
    method_note: str = Field(min_length=1)
    breakdown: UncertaintyBreakdown

    @model_validator(mode="after")
    def _kind_fields(self):
        if self.kind == "regression":
            if self.interval_low is None or self.interval_high is None or self.interval_low > self.interval_high:
                raise ValueError("regression requires an ordered interval")
        elif self.probability is None:
            raise ValueError("classification requires a calibrated probability")
        return self


class CompoundSubmission(Strict):
    smiles: list[str] = Field(min_length=1, max_length=MAX_COMPOUNDS)
    ensemble_n: Literal[3, 5, 10, 20] = DEFAULT_N
    endpoints: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _smiles_sane(self):
        for s in self.smiles:
            if not s.strip() or len(s) > 2000:
                raise ValueError("SMILES must be 1-2000 characters")
        return self


class PropertyWeight(Strict):
    endpoint: str
    weight: float = Field(ge=0)
    higher_is_better: bool


class HardLimit(Strict):
    endpoint: str
    min: Optional[float] = None
    max: Optional[float] = None
    use_unfavorable_bound: bool = False


class RankingConfig(Strict):
    weights: list[PropertyWeight] = Field(min_length=1)
    limits: list[HardLimit] = []
    lam: float = Field(default=DEFAULT_LAMBDA, ge=0, le=1)  # 0 = property only, 1 = confidence only

    @model_validator(mode="after")
    def _some_weight(self):
        if sum(w.weight for w in self.weights) <= 0:
            raise ValueError("at least one weight must be positive")
        return self


def allowed_ensemble_sizes(tier: str) -> tuple[int, ...]:
    cap = TIER_MAX_N[tier]
    return tuple(n for n in ENSEMBLE_SIZES if n <= cap)
