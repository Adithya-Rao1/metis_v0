from ml.data.clean import clean_dataset, standardize


def test_standardize_fragment_and_charge():
    smi, key = standardize("CC(=O)[O-].[Na+]")
    assert smi == "CC(=O)O"
    assert standardize("not a smiles") is None


def test_regression_cleaning_counts():
    recs = [("CCO", 1.0), ("OCC", 1.2), ("c1ccccc1", 0.0), ("c1ccccc1", 3.0), ("bad(", 1.0), ("CC(=O)[O-].[Na+]", 2.0), ("CC(=O)O", 2.1)]
    out, rep = clean_dataset(recs, "regression", tolerance=0.5)
    assert rep.n_input == 7 and rep.n_unparseable == 1
    assert rep.n_contradictory_dropped == 1 and rep.n_merged_duplicates == 2
    assert rep.n_output == len(out) == 2
    ethanol = [o for o in out if o["smiles"] == "CCO"][0]
    assert abs(ethanol["label"] - 1.1) < 1e-9


def test_classification_disagreement_dropped():
    out, rep = clean_dataset([("CCO", 1), ("OCC", 0), ("CCN", 1), ("NCC", 1)], "classification")
    assert rep.n_contradictory_dropped == 1 and rep.n_output == 1 and out[0]["label"] == 1.0


def test_stereo_kept_by_default():
    out, _ = clean_dataset([("C[C@H](N)C(=O)O", 1), ("C[C@@H](N)C(=O)O", 0)], "classification")
    assert len(out) == 2
    out, rep = clean_dataset([("C[C@H](N)C(=O)O", 1), ("C[C@@H](N)C(=O)O", 0)], "classification", keep_stereo=False)
    assert rep.n_contradictory_dropped == 1 and out == []


import math

import pytest


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None, "abc"])
def test_f29_non_finite_labels_dropped_and_counted(bad):
    out, rep = clean_dataset([("CCO", bad), ("OCC", bad), ("CCN", 2.0)], "regression")
    assert [o["smiles"] for o in out] == ["CCN"] and all(math.isfinite(o["label"]) for o in out)
    assert rep.n_non_finite_dropped == 2 and rep.reconciles()


def test_f30_report_reconciles_with_every_removal_kind():
    recs = [("CCO", 0), ("OCC", 1), ("CCN", 1), ("CCN", 1), ("bad", 1), ("", 1), ("CCC", float("nan")), ("c1ccccc1", 1)]
    out, r = clean_dataset(recs, "classification")
    assert r.n_input == 8 and r.n_contradictory_records == 2 and r.n_contradictory_dropped == 1
    assert r.n_input == (r.n_unparseable + r.n_non_finite_dropped + r.n_empty_after_standardize
                         + r.n_contradictory_records + r.n_merged_duplicates + r.n_output)
    assert r.reconciles()


def test_f30_empty_after_standardize_counted():
    out, r = clean_dataset([("[Na+]", 1.0), ("CCO", 1.0)], "regression")
    assert r.reconciles() and r.n_output == len(out)
