"""Leakage-safe splits. No random-only split is offered (spec: never quote random-split results)."""
from __future__ import annotations

import numpy as np
from rdkit import Chem, DataStructs
from rdkit.Chem import AllChem
from rdkit.Chem.Scaffolds import MurckoScaffold
from rdkit.ML.Cluster import Butina

SPLIT_METHODS = ("scaffold", "butina", "time")


def _assign_groups(groups: list[list[int]], n: int, frac: tuple[float, float, float], seed: int):
    """Whole groups go to exactly one partition (deepchem-style greedy: biggest groups fill train
    first; a group that would overflow a partition spills to the next). Seed breaks size ties."""
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(groups))
    groups = sorted((groups[i] for i in order), key=len, reverse=True)
    n_train, n_cal = frac[0] * n, (frac[0] + frac[1]) * n
    train, cal, test = [], [], []
    for g in groups:
        if len(train) + len(g) <= n_train:
            train += g
        elif len(train) + len(cal) + len(g) <= n_cal:
            cal += g
        else:
            test += g
    return sorted(train), sorted(cal), sorted(test)


def scaffold_split(smiles: list[str], frac=(0.8, 0.1, 0.1), seed: int = 0):
    groups: dict[str, list[int]] = {}
    for i, s in enumerate(smiles):
        mol = Chem.MolFromSmiles(s)
        scaf = MurckoScaffold.MurckoScaffoldSmiles(mol=mol) if mol else ""
        groups.setdefault(scaf, []).append(i)
    return _assign_groups(list(groups.values()), len(smiles), frac, seed)


def butina_split(smiles: list[str], frac=(0.8, 0.1, 0.1), seed: int = 0, threshold: float = 0.6):
    """threshold is Tanimoto DISTANCE; compounds closer than it share a cluster."""
    fps = [AllChem.GetMorganFingerprintAsBitVect(Chem.MolFromSmiles(s), 2, 2048) for s in smiles]
    dists = []
    for i in range(1, len(fps)):
        sims = DataStructs.BulkTanimotoSimilarity(fps[i], fps[:i])
        dists.extend(1 - x for x in sims)
    clusters = Butina.ClusterData(dists, len(fps), threshold, isDistData=True)
    return _assign_groups([list(c) for c in clusters], len(smiles), frac, seed)


def time_split(dates: list, frac=(0.8, 0.1, 0.1)):
    """Oldest to train, newest to test. Requires a date for every compound."""
    if any(d is None for d in dates):
        raise ValueError("time split requires dates for all compounds")
    order = sorted(range(len(dates)), key=lambda i: (dates[i], i))
    n = len(order)
    a, b = int(round(frac[0] * n)), int(round((frac[0] + frac[1]) * n))
    return sorted(order[:a]), sorted(order[a:b]), sorted(order[b:])


def split(method: str, smiles: list[str], dates: list | None = None, **kw):
    if method == "scaffold":
        return scaffold_split(smiles, **kw)
    if method == "butina":
        return butina_split(smiles, **kw)
    if method == "time":
        if dates is None:
            raise ValueError("time split needs dates")
        return time_split(dates, **kw)
    raise ValueError(f"unsupported split method {method!r}; allowed: {SPLIT_METHODS}")
