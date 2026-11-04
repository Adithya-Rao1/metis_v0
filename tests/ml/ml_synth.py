"""Synthetic data for tests only: generated molecules and labels. No real datasets anywhere."""
import numpy as np
from rdkit import Chem
from rdkit.Chem import Crippen

CORES = ["c1ccccc1", "c1ccncc1", "C1CCCCC1", "c1ccc2[nH]ccc2c1", "c1ccsc1", "C1CCNCC1", "c1ccc2ccccc2c1", "c1ccoc1"]
SUBS = ["C", "CC", "O", "N", "F", "Cl", "C(=O)O", "OC", "C#N", "CCO", "CCCC", "C(=O)N", "CCN", "Br", "CC(C)C"]
OOD = ["O=S(=O)(O)CCCCCCCCP(=O)(O)O", "C[Si](C)(C)O[Si](C)(C)C", "OCC(O)C(O)C(O)C(O)CO"]


def make_molecules(n: int, seed: int = 0) -> list[str]:
    rng = np.random.default_rng(seed)
    out, seen = [], set()
    while len(out) < n:
        s = rng.choice(SUBS) + rng.choice(CORES) + rng.choice(SUBS)
        m = Chem.MolFromSmiles(s)
        if m is None:
            continue
        c = Chem.MolToSmiles(m)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def regression_labels(smiles, seed=0, noise=0.2):
    rng = np.random.default_rng(seed + 1)
    return np.array([Crippen.MolLogP(Chem.MolFromSmiles(s)) + 0.5 * s.count("N") for s in smiles]) + rng.normal(0, noise, len(smiles))


def classification_labels(smiles, seed=0):
    x = np.array([Crippen.MolLogP(Chem.MolFromSmiles(s)) for s in smiles])
    y = (x > np.median(x)).astype(int)
    flip = np.random.default_rng(seed + 2).random(len(y)) < 0.1
    return np.where(flip, 1 - y, y)
