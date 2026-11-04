import pytest
from rdkit import Chem, DataStructs
from rdkit.Chem import AllChem
from rdkit.Chem.Scaffolds import MurckoScaffold
from ml.data import splits
from ml.data.splits import scaffold_split, butina_split, time_split

CORES = ["c1ccccc1", "c1ccncc1", "C1CCCCC1", "c1ccc2ccccc2c1", "c1ccsc1", "C1CCNCC1", "c1ccoc1", "C1CCOC1", "c1ccc2[nH]ccc2c1", "c1ccc2occc2c1"]
SUBS = ["", "C", "CC", "O", "N", "Cl", "F", "CCC"]
SMILES = []
for core in CORES:
    for sub in SUBS:
        # substituent attached at the first ring atom
        SMILES.append(core.replace("1", "1", 1) if not sub else core[0] + "(" + sub + ")" + core[1:])
SMILES = [s for s in SMILES if Chem.MolFromSmiles(s)]


def scaf(s):
    return MurckoScaffold.MurckoScaffoldSmiles(mol=Chem.MolFromSmiles(s))


def test_enough_fixture():
    assert len(SMILES) >= 60


def test_scaffold_no_leakage():
    tr, ca, te = scaffold_split(SMILES, seed=1)
    assert sorted(tr + ca + te) == list(range(len(SMILES)))
    assert te and ca
    s = [{scaf(SMILES[i]) for i in part} for part in (tr, ca, te)]
    assert not (s[0] & s[1]) and not (s[0] & s[2]) and not (s[1] & s[2])


def test_butina_no_cluster_leakage():
    thr = 0.6
    tr, ca, te = butina_split(SMILES, threshold=thr, seed=2)
    assert sorted(tr + ca + te) == list(range(len(SMILES)))
    assert te and ca
    fps = [AllChem.GetMorganFingerprintAsBitVect(Chem.MolFromSmiles(s), 2, 2048) for s in SMILES]
    # Butina members are within threshold of their centroid; entire clusters stay together, so
    # check partition membership is a union of clusters via recomputation.
    from rdkit.ML.Cluster import Butina
    d = []
    for i in range(1, len(fps)):
        d.extend(1 - x for x in DataStructs.BulkTanimotoSimilarity(fps[i], fps[:i]))
    part = {i: k for k, p in enumerate((tr, ca, te)) for i in p}
    for c in Butina.ClusterData(d, len(fps), thr, isDistData=True):
        assert len({part[i] for i in c}) == 1


def test_time_split_order_and_missing():
    dates = [2020 + (i % 7) for i in range(50)]
    tr, ca, te = time_split(dates)
    assert max(dates[i] for i in tr) <= min(dates[i] for i in ca) <= max(dates[i] for i in ca) <= min(dates[i] for i in te)
    with pytest.raises(ValueError):
        time_split([2020, None, 2021])


def test_no_random_option():
    assert "random" not in splits.SPLIT_METHODS
    with pytest.raises(ValueError):
        splits.split("random", SMILES)
    with pytest.raises(ValueError):
        splits.split("time", SMILES)
    import inspect
    for fn in (scaffold_split, butina_split, time_split):
        assert "random" not in inspect.getsource(fn).lower().replace("default_rng", "")


ACYCLIC = ["C" * n for n in range(2, 12)]
RINGS = ["c1ccccc1", "C1CCCCC1", "c1ccncc1", "C1CCNCC1", "c1ccoc1", "c1ccsc1", "C1CCOC1", "C1CCCC1", "C1CC1", "C1CCC1"]


def test_f31_acyclic_molecules_do_not_empty_train():
    tr, ca, te = scaffold_split(ACYCLIC)
    assert len(tr) > 0 and len(ca) > 0 and len(te) > 0 and sorted(tr + ca + te) == list(range(10))


def test_f31_dominant_ring_scaffold_goes_to_train_or_clear_error():
    smi = ["c1ccccc1" + "C" * i for i in range(1, 19)] + ["C1CCCCC1", "c1ccncc1"]
    try:
        tr, ca, te = scaffold_split(smi)
    except ValueError as e:
        assert "empty" in str(e)
        return
    assert len(tr) > 0 and len(ca) > 0 and len(te) > 0


@pytest.mark.parametrize("smi", [RINGS[:1], RINGS[:2], []])
def test_f32_tiny_inputs_raise(smi):
    with pytest.raises(ValueError, match="empty|train"):
        scaffold_split(smi)


@pytest.mark.parametrize("fn", [scaffold_split, butina_split])
def test_f33_unparseable_smiles_named_by_index_only(fn):
    with pytest.raises(ValueError, match="index 1") as ei:
        fn(["CCO", "not_a_smiles", "CCN"])
    assert "not_a_smiles" not in str(ei.value)


@pytest.mark.parametrize("frac", [(0.9, 0.9, 0.9), (-1, 0, 2), (0, 0, 0), (0, 0.5, 0.5), (float("nan"), 0.5, 0.5)])
def test_f34_fractions_validated(frac):
    with pytest.raises(ValueError):
        scaffold_split(RINGS * 2, frac=frac)
    with pytest.raises(ValueError):
        time_split(list(range(20)), frac=frac)


def test_f35_time_split_groups_stereoisomers():
    smi = ["CCO", "c1ccccc1C", "CCN", "CCC", "CCCC", "CCCCC", "CCCCCC", "CCCCCCC", "N[C@@H](C)C(=O)O", "N[C@H](C)C(=O)O"]
    tr, ca, te = splits.split("time", smi, dates=list(range(10)))
    assert sorted(tr + ca + te) == list(range(10)) and all(len(p) for p in (tr, ca, te))
    assert {8, 9} <= set(tr) or {8, 9} <= set(ca) or {8, 9} <= set(te)


def test_time_split_without_smiles_unchanged():
    assert time_split(list(range(10))) == ([0, 1, 2, 3, 4, 5, 6, 7], [8], [9])


def test_time_split_raises_when_partition_empty():
    with pytest.raises(ValueError):
        time_split([1, 2])


def test_f32_small_inputs_fill_every_partition_when_possible():
    parts = scaffold_split(RINGS[:5])
    assert all(len(p) > 0 for p in parts) and sorted(sum(parts, [])) == list(range(5))


def test_f32_three_equal_groups_each_get_a_partition():
    smi = [s + "C" * i for s in ("c1ccccc1", "C1CCCCC1", "c1ccncc1") for i in range(1, 11)]
    parts = scaffold_split(smi)
    assert all(len(p) > 0 for p in parts)
