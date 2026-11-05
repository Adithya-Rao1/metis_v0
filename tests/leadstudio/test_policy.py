import pytest
from rdkit import Chem

from leadstudio.contracts import Policy
from leadstudio.policy.features import ATOM_DIM, BOND_DIM, mol_graph
from leadstudio.policy.policy import UniformPolicy, load_policy
from leadstudio.registry import get_registry
from leadstudio.wiring import FrozenPolicy

SMI = "CCOC(=O)c1ccc(cc1)[N+](=O)[O-]"


def test_features_shape():
    x, ei, ea = mol_graph("CCO")
    assert x.shape == (3, ATOM_DIM) and ei.shape == (2, 4) and ea.shape == (4, BOND_DIM)


def test_features_invalid_smiles_raises():
    with pytest.raises(ValueError):
        mol_graph("not a smiles")


def test_uniform_policy_protocol():
    reg = get_registry()
    cands = reg.applicable(SMI, [])
    assert cands
    pol = UniformPolicy()
    assert isinstance(pol, Policy)
    assert pol.rank(SMI, cands) == [0.0] * len(cands)
    assert pol.rank(SMI, []) == []
    assert pol.update([(SMI, "x", [0], "CC", 0.5)]) is None


def test_load_policy_without_checkpoint_is_uniform_and_does_not_need_torch(tmp_path):
    assert isinstance(load_policy(["a", "b"], checkpoint=str(tmp_path / "missing.pt")), UniformPolicy)
    assert isinstance(load_policy(["a", "b"]), UniformPolicy)  # no checkpoint ships with the service


def test_unusable_checkpoint_falls_back_to_uniform(tmp_path):
    bad = tmp_path / "p.pt"
    bad.write_bytes(b"not a checkpoint")
    assert isinstance(load_policy(["a"], checkpoint=str(bad)), UniformPolicy)


def test_frozen_policy_drops_updates():
    class Learner:
        checkpoint_loaded = None

        def __init__(self):
            self.updates = 0

        def rank(self, smiles, candidates):
            return [1.0] * len(candidates)

        def update(self, transitions):
            self.updates += 1
            return 0.1

    inner = Learner()
    fp = FrozenPolicy.wrap(inner)
    assert fp.rank(SMI, [1, 2]) == [1.0, 1.0]
    assert fp.update([(SMI, "x", [0], "CC", 1.0)]) is None
    assert inner.updates == 0


def test_dqn_policy_roundtrip(tmp_path):
    pytest.importorskip("torch", reason="DQN policy is optional and needs torch")
    from leadstudio.policy.dqn import DQNPolicy

    reg = get_registry()
    ids = [o.id for o in reg.all()]
    p = DQNPolicy(ids, checkpoint=None)
    cands = reg.applicable(SMI, [])
    op, site = cands[0]
    for _ in range(10):
        p.update([(SMI, op.id, site, "CC", 0.5)])
    path = str(tmp_path / "p.pt")
    p.save(path)
    q = DQNPolicy(ids, checkpoint=path)
    assert q.checkpoint_loaded == path
    assert q.rank(SMI, cands) == p.rank(SMI, cands)
    assert Chem.MolFromSmiles(SMI) is not None
