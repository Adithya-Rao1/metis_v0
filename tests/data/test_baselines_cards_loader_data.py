import numpy as np
import pytest
from ml.data.baselines import rf_baseline, rf_predict, gse_logs, crippen_logp, GSE_NOTE
from ml.data.cards import render_card, write_card
from ml.data.loader import load_local, main
from ml.data.manifest import LicenseError

S = ["CCO", "CCCO", "CCCCO", "c1ccccc1", "Cc1ccccc1", "CCc1ccccc1", "CCN", "CCCN", "CCCCN", "c1ccncc1"]


def test_rf_regression_and_classification():
    y = np.array([crippen_logp(s) for s in S])
    m = rf_baseline(S, y, "regression", n_estimators=20)
    assert rf_predict(m, S, "regression").shape == (10,)
    c = rf_baseline(S, [int("c1" in s) for s in S], "classification", n_estimators=20)
    p = rf_predict(c, S, "classification")
    assert ((0 <= p) & (p <= 1)).all()


def test_gse_and_crippen():
    v, used = gse_logs("CCCCCC")
    assert not used and abs(v - (0.5 - crippen_logp("CCCCCC"))) < 1e-9
    v2, used2 = gse_logs("CCCCCC", 125.0)
    assert used2 and abs(v2 - (v - 1.0)) < 1e-9
    assert "missing" in GSE_NOTE


def test_card_content():
    t = render_card("AMES", 100, "scaffold", (80, 10, 10))
    for w in ["Size", "Units", "scaffold", "pending", "NOT cleared", "Limitations", "TDC", "Xu et al."]:
        assert w in t
    assert "EXCLUDED" in render_card("FreeSolv", None, None)
    with pytest.raises(ValueError):
        render_card("AMES", 10, "random")


def test_write_card(tmp_path):
    assert write_card("hERG", out_dir=tmp_path).read_text().startswith("# Data card")


def test_loader_cache_and_gates(tmp_path):
    (tmp_path / "AMES.csv").write_text("Drug_ID,Drug,Y\na,CCO,1\nb,CCN,0\n")
    assert load_local("AMES", tmp_path) == [("CCO", 1.0), ("CCN", 0.0)]
    with pytest.raises(FileNotFoundError):
        load_local("hERG", tmp_path)
    with pytest.raises(LicenseError):
        load_local("FreeSolv", tmp_path)


def test_cli_does_not_download_without_flag(capsys):
    assert main(["AMES"]) == 1
    assert main(["--list"]) == 0
    with pytest.raises(LicenseError):
        main(["FreeSolv", "--download"])


from ml.data.loader import cache_path


@pytest.mark.parametrize("m", ["Random", "RANDOM", "random ", "random", "stratified_random", "kfold"])
def test_f28_card_rejects_non_allowed_splits(m):
    with pytest.raises(ValueError):
        render_card("hERG", 10, m)


@pytest.mark.parametrize("bad", ["../../evil", "a/b", "x.csv", "", "a b"])
def test_f27_cache_path_rejects_bad_ids(tmp_path, bad):
    with pytest.raises(ValueError):
        cache_path(bad, tmp_path)


def test_f27_cache_path_ok(tmp_path):
    assert cache_path("hERG", tmp_path) == tmp_path / "hERG.csv"


@pytest.mark.parametrize("cell", ["nan", "inf", "-inf"])
def test_f29b_load_local_rejects_non_finite(tmp_path, cell):
    (tmp_path / "hERG.csv").write_text(f"Drug,Y\nCCO,1\nCCN,{cell}\n")
    with pytest.raises(ValueError, match="row 2"):
        load_local("hERG", tmp_path)


def test_f36_single_class_classifier_predicts():
    m = rf_baseline(["CCO", "CCN", "CCC"], np.array([0, 0, 0]), "classification", n_estimators=5)
    assert rf_predict(m, ["CCO"], "classification").tolist() == [0.0]
    m = rf_baseline(["CCO", "CCN", "CCC"], np.array([1, 1, 1]), "classification", n_estimators=5)
    assert rf_predict(m, ["CCO"], "classification").tolist() == [1.0]
