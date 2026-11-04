import numpy as np
import pytest
from rdkit import Chem

from ml.train.ensemble import EnsemblePool
from ml.train.member import SklearnMember
from ml.uncertainty.attribution import NOTE, attribute, fragment_atoms, mask_fragment


@pytest.fixture(scope="module")
def pool(mols, yreg):
    return EnsemblePool(SklearnMember, "regression", {"n_estimators": 10}, 6, 0).train(mols[:150], yreg[:150])


SMI = "CCOc1ccc(CC(=O)NC2CCNCC2)cc1"


def run(pool, seed=0, smi=SMI, train=None):
    return attribute(smi, lambda ss: pool.predict_members(list(ss)), train, seed=seed)


def test_shares_sum_to_one_and_ranges(pool, mols):
    r = run(pool, train=mols[:100])
    assert len(r.fragments) > 1
    assert abs(sum(f.share for f in r.fragments) - 1) < 1e-9
    for f in r.fragments:
        assert 0 <= f.share_low <= f.share <= f.share_high <= 1
        assert 0 <= f.train_frequency <= 1


def test_deterministic_for_seed(pool, mols):
    a, b = run(pool, 5, train=mols[:50]), run(pool, 5, train=mols[:50])
    assert [f.model_dump() for f in a.fragments] == [f.model_dump() for f in b.fragments]


def test_atom_indices_map_to_input_molecule(pool):
    mol = Chem.MolFromSmiles(SMI)
    r = run(pool)
    all_idx = [i for f in r.fragments for i in f.atom_indices]
    assert sorted(all_idx) == list(range(mol.GetNumAtoms()))  # partition of the input atoms
    for f in r.fragments:
        assert f.atom_indices == sorted(f.atom_indices)
    assert [f.atom_indices for f in r.fragments] == fragment_atoms(mol)


def test_labelled_approximate(pool):
    r = run(pool)
    assert "approximate" in r.status and "Approximate" in r.note and r.note == NOTE


def test_no_brics_bond_gives_single_fragment(pool):
    r = run(pool, smi="c1ccccc1")
    assert len(r.fragments) == 1 and r.fragments[0].share == 1.0


def test_mask_fragment_failure_and_success():
    mol = Chem.MolFromSmiles("CCO")
    assert mask_fragment(mol, [0, 1, 2]) is None
    assert Chem.MolFromSmiles(mask_fragment(mol, [2])) is not None


def test_train_frequency_counts_substructure(pool):
    r = run(pool, smi="CCc1ccccc1", train=["c1ccccc1C", "CCCC", "c1ccccc1O", "CC"])
    assert max(f.train_frequency for f in r.fragments) > 0
