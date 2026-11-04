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
