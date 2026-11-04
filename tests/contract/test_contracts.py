import uuid
import pytest
from pydantic import ValidationError
from backend.schemas.contracts import *

BD = dict(ensemble_disagreement=0.5, similarity_to_training=0.3, assay_noise=0.2)
BASE = dict(endpoint="solubility", kind="regression", model_version_id=uuid.uuid4(), ensemble_n=5, value=1.0,
            interval_low=0.5, interval_high=1.5, coverage=0.9, sample_size=100, method_note="split conformal", breakdown=BD)


def test_valid_prediction():
    Prediction(**BASE)


@pytest.mark.parametrize("drop", ["coverage", "sample_size", "method_note", "ensemble_n", "breakdown"])
def test_required_fields(drop):
    d = {k: v for k, v in BASE.items() if k != drop}
    with pytest.raises(ValidationError):
        Prediction(**d)


def test_no_verdict_fields_accepted():
    for extra in ("verdict", "traffic_light", "status", "good"):
        with pytest.raises(ValidationError):
            Prediction(**BASE, **{extra: "green"})


def test_regression_needs_ordered_interval_and_classification_needs_probability():
    with pytest.raises(ValidationError):
        Prediction(**{**BASE, "interval_low": 2.0})
    with pytest.raises(ValidationError):
        Prediction(**{**BASE, "interval_low": None})
    with pytest.raises(ValidationError):
        Prediction(**{**BASE, "kind": "classification"})
    Prediction(**{**BASE, "kind": "classification", "probability": 0.2, "interval_low": None, "interval_high": None})


def test_ensemble_sizes():
    with pytest.raises(ValidationError):
        Prediction(**{**BASE, "ensemble_n": 7})
    assert allowed_ensemble_sizes("free") == (3, 5, 10)
    assert allowed_ensemble_sizes("edu") == (3, 5, 10, 20)


def test_breakdown_sums_to_one():
    with pytest.raises(ValidationError):
        UncertaintyBreakdown(ensemble_disagreement=0.5, similarity_to_training=0.5, assay_noise=0.5)


def test_ten_compound_cap():
    CompoundSubmission(smiles=[f"C{'C'*i}" for i in range(10)], endpoints=["solubility"])
    with pytest.raises(ValidationError):
        CompoundSubmission(smiles=[f"C{'C'*i}" for i in range(11)], endpoints=["solubility"])
    with pytest.raises(ValidationError):
        CompoundSubmission(smiles=[], endpoints=["solubility"])
    with pytest.raises(ValidationError):
        CompoundSubmission(smiles=["C"], endpoints=["x"], ensemble_n=4)


def test_ranking_config_defaults_and_bounds():
    w = [PropertyWeight(endpoint="sol", weight=1, higher_is_better=True)]
    assert RankingConfig(weights=w).lam == 0.25
    with pytest.raises(ValidationError):
        RankingConfig(weights=w, lam=1.5)
    with pytest.raises(ValidationError):
        RankingConfig(weights=[PropertyWeight(endpoint="sol", weight=0, higher_is_better=True)])
