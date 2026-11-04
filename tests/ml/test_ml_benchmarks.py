import json

import numpy as np
import pytest

from ml.benchmarks.protocol import default_split_methods, run_benchmark, run_protocol
from ml.train.member import SklearnMember

KW = {"member_cls": SklearnMember, "member_params": {"n_estimators": 8}, "n_members": 20, "rf_trees": 10}


def test_default_splits_never_random():
    assert default_split_methods(True) == ["time"] and default_split_methods(False) == ["butina", "scaffold"]


def test_rejects_random_split(mols, yreg):
    with pytest.raises(ValueError):
        run_benchmark(mols, yreg, "regression", split_method="random", **KW)


def test_regression_report(mols, yreg, tmp_path):
    out = tmp_path / "r.json"
    rep = run_benchmark(mols, yreg, "regression", split_method="scaffold", noise_sd=0.3, endpoint="solubility", out_path=out, **KW)
    assert json.loads(out.read_text())["split"] == "scaffold"
    assert set(rep["metrics"]) == {"ensemble", "rf_morgan", "gse"}
    assert set(rep["metrics"]["ensemble"]) == {"rmse", "mae", "r2"}
    assert rep["noise_ceiling"]["ensemble_rmse_over_ceiling"] > 0
    assert 0 <= rep["paired_5x2cv"]["p_value"] <= 1 and rep["paired_5x2cv"]["df"] == 5
    assert set(rep["coverage_by_n"]) == {"3", "5", "10", "20"}
    for v in rep["coverage_by_n"].values():
        assert 0 <= v["coverage"] <= 1 and len(v["by_similarity_bin"]) == 4
    assert set(rep["runtime_seconds_per_compound_by_n"]) == {"3", "5", "10", "20"}
    assert all(v > 0 for v in rep["runtime_seconds_per_compound_by_n"].values())
    assert rep["random_split_results"] == "never reported"


def test_noise_unknown_is_stated(mols, yreg):
    rep = run_benchmark(mols, yreg, "regression", split_method="butina", run_cv=False, **KW)
    assert rep["noise_ceiling"]["status"] == "unknown"


def test_classification_report(mols, ycls):
    rep = run_benchmark(mols, ycls, "classification", split_method="butina", run_cv=True, **KW)
    m = rep["metrics"]["ensemble"]
    assert set(m) >= {"auroc", "mcc", "auprc"} and m["auroc"] > 0.5
    r = rep["reliability_by_n"]["5"]
    assert set(r) == {"raw", "isotonic", "venn_abers"} and r["raw"]["ece"] is not None


def test_time_split_with_dates_and_protocol(mols, yreg, tmp_path):
    dates = list(range(len(mols)))
    reps = run_protocol(mols, yreg, "regression", dates=dates, out_dir=tmp_path, run_cv=False, **KW)
    assert list(reps) == ["time"] and (tmp_path / "endpoint_time.json").exists()
    reps = run_protocol(mols, yreg, "regression", out_dir=tmp_path, run_cv=False, **KW)
    assert list(reps) == ["butina", "scaffold"]
