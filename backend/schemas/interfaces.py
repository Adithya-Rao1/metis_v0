"""Frozen cross-agent interfaces (Wave 2). Integrator-owned; changes go through docs/decisions/.

- Accounts (Agent 4) provides `backend.accounts.deps.current_user` -> UserCtx (FastAPI dependency) and
  `backend.accounts.router` (APIRouter).
- Domain API (Agent 6) provides `backend.projects.router` and consumes a ModelRegistry.
- Models (Agent 5) provides a concrete ModelRegistry in `ml/registry/` (file-based, reads manifest.json).
- Docking prep (Agent 7) provides `backend.dockprep.router`.
`backend.main.create_app` mounts whichever of these routers import successfully.
"""
from __future__ import annotations

from typing import Literal, Optional, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .contracts import Prediction


class UserCtx(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    tier: Literal["free", "edu", "paid"]
    email_verified: bool


class ModelCard(BaseModel):
    """Everything a model page shows (spec 5, 6, 12). Published only if all fields are real."""
    model_config = ConfigDict(extra="forbid")
    endpoint: str
    version: str
    kind: Literal["regression", "classification"]
    units: Optional[str] = None
    datasets: list[dict] = Field(min_length=1)       # name, url, license, size, split
    limitations: str = Field(min_length=1)
    split_type: str
    benchmark: dict                                  # metrics vs baselines
    coverage_by_n: dict[str, float]                  # "3","5","10","20" -> empirical coverage
    runtime_by_n_seconds: dict[str, float]
    default_range: tuple[float, float]               # training-data min/max for desirability (decision 0002)
    weights_sha256: str


class FragmentShare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    atom_indices: list[int]
    share: float = Field(ge=0, le=1)
    share_low: float = Field(ge=0, le=1)
    share_high: float = Field(ge=0, le=1)
    train_frequency: float = Field(ge=0, le=1)


class PredictionDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prediction: Prediction
    fragments: list[FragmentShare] = []
    nearest_training: list[dict] = []                # smiles, similarity, label (no private user data)
    runtime_seconds: float = Field(ge=0)


@runtime_checkable
class ModelRegistry(Protocol):
    def list_models(self) -> list[ModelCard]: ...
    def model_card(self, endpoint: str) -> ModelCard: ...
    def time_estimate_seconds(self, endpoint: str, ensemble_n: int, n_compounds: int) -> float: ...
    def predict(self, endpoint: str, canonical_smiles: str, ensemble_n: int) -> PredictionDetail: ...
