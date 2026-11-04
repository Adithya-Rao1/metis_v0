"""File-based ModelRegistry (backend.schemas.interfaces.ModelRegistry). Reads ml/registry/manifest.json and the
git-ignored artifacts dir. Members are hash-verified against the manifest before they are deserialised."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from backend.schemas.contracts import ENSEMBLE_SIZES
from backend.schemas.interfaces import ModelCard, PredictionDetail
from ml.registry.predictor import predict_with_bundle
from ml.train.bundle import load_bundle

REGISTRY_ROOT = Path(__file__).resolve().parent


class NotTrained(RuntimeError):
    """No trained, published model exists for this endpoint (yet)."""

    def __init__(self, endpoint: str):
        super().__init__(f"no trained model is published for endpoint {endpoint!r}; predictions are unavailable (not trained)")
        self.endpoint = endpoint


class UntrainedRegistry:
    """Registry used until real models are published. Lists nothing; every call says NotTrained."""

    def list_models(self) -> list[ModelCard]:
        return []

    def model_card(self, endpoint: str) -> ModelCard:
        raise NotTrained(endpoint)

    def time_estimate_seconds(self, endpoint: str, ensemble_n: int, n_compounds: int) -> float:
        raise NotTrained(endpoint)

    def predict(self, endpoint: str, canonical_smiles: str, ensemble_n: int) -> PredictionDetail:
        raise NotTrained(endpoint)


class FileModelRegistry:
    def __init__(self, root: Path | str = REGISTRY_ROOT):
        self.root = Path(root)
        self._bundles: dict = {}

    def _manifest(self) -> dict:
        p = self.root / "manifest.json"
        if not p.exists():
            return {"models": {}}
        return json.loads(p.read_text())

    def _entry(self, endpoint: str) -> dict:
        e = self._manifest()["models"].get(endpoint)
        if e is None:
            raise NotTrained(endpoint)
        return e

    def list_models(self) -> list[ModelCard]:
        return [ModelCard(**e["card"]) for _, e in sorted(self._manifest()["models"].items())]

    def model_card(self, endpoint: str) -> ModelCard:
        return ModelCard(**self._entry(endpoint)["card"])

    def time_estimate_seconds(self, endpoint: str, ensemble_n: int, n_compounds: int) -> float:
        """Measured formula: fixed_overhead + n_compounds * seconds_per_compound[N] (both measured at publish time,
        per-compound cost includes fragment attribution). Machine-specific: re-measured on the serving host if it differs."""
        if ensemble_n not in ENSEMBLE_SIZES:
            raise ValueError(f"ensemble_n must be one of {ENSEMBLE_SIZES}")
        if n_compounds < 1:
            raise ValueError("n_compounds must be >= 1")
        rt = self._entry(endpoint)["runtime"]
        return float(rt["fixed_overhead_seconds"] + n_compounds * rt["seconds_per_compound_by_n"][str(ensemble_n)])

    def _bundle(self, endpoint: str):
        e = self._entry(endpoint)
        key = (endpoint, e["weights_sha256"])
        if key not in self._bundles:
            self._bundles[key] = load_bundle(self.root / e["bundle_dir"], expected_weights_sha256=e["weights_sha256"])
        return self._bundles[key], UUID(e["model_version_id"]), e

    def predict(self, endpoint: str, canonical_smiles: str, ensemble_n: int) -> PredictionDetail:
        if ensemble_n not in ENSEMBLE_SIZES:
            raise ValueError(f"ensemble_n must be one of {ENSEMBLE_SIZES}")
        b, vid, e = self._bundle(endpoint)
        hint = e["card"]["coverage_by_n"].get(str(ensemble_n))
        return predict_with_bundle(b, vid, canonical_smiles, ensemble_n, coverage_hint=hint)


def default_registry(root: Path | str = REGISTRY_ROOT):
    """FileModelRegistry if anything is published, else the explicit untrained stub."""
    reg = FileModelRegistry(root)
    return reg if reg._manifest()["models"] else UntrainedRegistry()
