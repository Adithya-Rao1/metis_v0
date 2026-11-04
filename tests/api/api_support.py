"""Shared test helpers for tests/api (kept out of conftest so tests can import them without the `tests` package)."""
from __future__ import annotations

import hashlib
import uuid

from backend.schemas.contracts import Prediction, UncertaintyBreakdown
from backend.schemas.interfaces import FragmentShare, ModelCard, PredictionDetail, UserCtx


MODEL_VERSION = uuid.UUID("11111111-1111-1111-1111-111111111111")
CARDS = {
    # test-only model cards; nothing here is a real model
    "logd": dict(kind="regression", units="log units", default_range=(-2.0, 6.0), mw=1.0),
    "sol": dict(kind="regression", units="log mol/L", default_range=(-8.0, 0.0), mw=2.0),
    "ames": dict(kind="classification", units=None, default_range=(0.0, 1.0), mw=None),
}


class FakeRegistry:
    """In-test registry. Values are derived from a hash so they are deterministic and obviously synthetic."""

    version_id = MODEL_VERSION

    def _card(self, e: str) -> ModelCard:
        if e not in CARDS:
            raise KeyError(e)
        c = CARDS[e]
        bench = {"test_only": True}
        if c["mw"]:
            bench["median_calibration_width"] = c["mw"]
        return ModelCard(
            endpoint=e, version="test-0", kind=c["kind"], units=c["units"],
            datasets=[{"name": "test", "url": "https://example.invalid", "license": "test", "size": 1, "split": "scaffold"}],
            limitations="Test model for unit tests only.", split_type="scaffold", benchmark=bench,
            coverage_by_n={"3": 0.9, "5": 0.9, "10": 0.9, "20": 0.9}, runtime_by_n_seconds={"3": 1, "5": 2, "10": 4, "20": 8},
            default_range=c["default_range"], weights_sha256="0" * 64,
        )

    def list_models(self):
        return [self._card(e) for e in CARDS]

    def model_card(self, endpoint):
        return self._card(endpoint)

    def time_estimate_seconds(self, endpoint, ensemble_n, n_compounds):
        self._card(endpoint)
        return 0.5 * ensemble_n * n_compounds

    def predict(self, endpoint, canonical_smiles, ensemble_n):
        card = self._card(endpoint)
        h = int(hashlib.sha256(f"{endpoint}{canonical_smiles}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        lo_r, hi_r = card.default_range
        bd = UncertaintyBreakdown(ensemble_disagreement=0.5, similarity_to_training=0.3, assay_noise=0.2)
        if card.kind == "regression":
            v = lo_r + h * (hi_r - lo_r)
            p = Prediction(endpoint=endpoint, kind="regression", model_version_id=self.version_id, ensemble_n=ensemble_n, value=v,
                           interval_low=v - 0.5, interval_high=v + 0.5, coverage=0.9, sample_size=1000, method_note="test", breakdown=bd)
        else:
            p = Prediction(endpoint=endpoint, kind="classification", model_version_id=self.version_id, ensemble_n=ensemble_n, value=h,
                           probability=h, coverage=0.9, sample_size=1000, method_note="test", breakdown=bd)
        return PredictionDetail(prediction=p, fragments=[FragmentShare(atom_indices=[0], share=0.5, share_low=0.4, share_high=0.6, train_frequency=0.1)],
                                nearest_training=[{"smiles": "CC", "similarity": 0.5, "label": 1.0}], runtime_seconds=0.1)




def H(who: str) -> dict:
    return {"X-Test-User": who}
