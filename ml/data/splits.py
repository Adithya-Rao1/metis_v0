"""Leakage-safe splits. No random-only split is offered (spec: never quote random-split results)."""

from __future__ import annotations

import numpy as np
from rdkit import Chem, DataStructs
from rdkit.Chem import AllChem, inchi
from rdkit.Chem.Scaffolds import MurckoScaffold
from rdkit.ML.Cluster import Butina

SPLIT_METHODS = ("scaffold", "butina", "time")


def _validate_frac(frac) -> tuple[float, float, float]:
    try:
        f = tuple(float(x) for x in frac)
    except (TypeError, ValueError):
        raise ValueError("frac must be three numbers") from None
    if len(f) != 3 or any(not np.isfinite(x) or x < 0 for x in f):
        raise ValueError("frac must be three non-negative finite numbers")
    if abs(sum(f) - 1.0) > 1e-6:
        raise ValueError(f"frac must sum to 1, got {sum(f)}")
    if f[0] <= 0:
        raise ValueError("train fraction must be > 0")
    return f


def _check_parseable(smiles: list[str]) -> list:
    mols = []
    for i, s in enumerate(smiles):
        m = Chem.MolFromSmiles(s) if isinstance(s, str) else None
        if m is None:
            raise ValueError(f"unparseable SMILES at index {i}")
        mols.append(m)
    return mols


def _check_nonempty(parts, frac) -> None:
    names = ("train", "cal", "test")
    for name, p, f in zip(names, parts, frac):
        if f > 0 and len(p) == 0:
            raise ValueError(
                f"split would leave {name} empty (n={sum(map(len, parts))}, frac={tuple(frac)}); "
                "need more compounds or more diverse groups"
            )


def _assign_groups(groups: list[list[int]], n: int, frac: tuple[float, float, float], seed: int):
    """Whole groups go to exactly one partition (deepchem-style greedy: biggest groups fill train
    first; a group that would overflow a partition spills to the next). A group bigger than the
    train budget still goes to train if train is empty (size skew: train may exceed its fraction).
    Raises ValueError if a partition with positive fraction would end up empty. Seed breaks size ties."""
    frac = _validate_frac(frac)
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(groups))
    groups = sorted((groups[i] for i in order), key=len, reverse=True)
    n_train, n_cal = frac[0] * n, (frac[0] + frac[1]) * n
    parts: list[list[list[int]]] = [[], [], []]  # per partition: list of groups
    size = [0, 0, 0]
    for g in groups:
        if not parts[0] or size[0] + len(g) <= n_train:
            k = 0
        elif frac[1] > 0 and size[0] + size[1] + len(g) <= n_cal:
            k = 1
        elif frac[2] > 0:
            k = 2
        else:
            k = 0
        parts[k].append(g)
        size[k] += len(g)
    # Fix-up: a partition with positive fraction must not be empty. Move the smallest group out of the
    # partition holding the most groups (never leaving it empty). Impossible only if groups < partitions.
    for k in (1, 2, 0):
        while frac[k] > 0 and not parts[k]:
            donors = [q for q in (2, 1, 0) if len(parts[q]) > 1]
            if not donors:
                break
            d = max(donors, key=lambda q: len(parts[q]))
            g = min(parts[d], key=len)
            parts[d].remove(g)
            parts[k].append(g)
    out = tuple(sorted(i for g in pg for i in g) for pg in parts)
    _check_nonempty(out, frac)
    return out


def scaffold_split(smiles: list[str], frac=(0.8, 0.1, 0.1), seed: int = 0):
    frac = _validate_frac(frac)
    mols = _check_parseable(smiles)
    groups: dict[str, list[int]] = {}
    acyclic: dict[str, list[int]] = {}
    for i, mol in enumerate(mols):
        scaf = MurckoScaffold.MurckoScaffoldSmiles(mol=mol)
        if scaf == "":
            # acyclic: no ring scaffold is shared, so split by connectivity (stereoisomers stay together)
            acyclic.setdefault(inchi.MolToInchiKey(mol).split("-")[0], []).append(i)
        else:
            groups.setdefault(scaf, []).append(i)
    return _assign_groups(list(groups.values()) + list(acyclic.values()), len(smiles), frac, seed)


def butina_split(smiles: list[str], frac=(0.8, 0.1, 0.1), seed: int = 0, threshold: float = 0.6):
    """threshold is Tanimoto DISTANCE; compounds closer than it share a cluster."""
    frac = _validate_frac(frac)
    mols = _check_parseable(smiles)
    fps = [AllChem.GetMorganFingerprintAsBitVect(m, 2, 2048) for m in mols]
    dists = []
    for i in range(1, len(fps)):
        sims = DataStructs.BulkTanimotoSimilarity(fps[i], fps[:i])
        dists.extend(1 - x for x in sims)
    clusters = Butina.ClusterData(dists, len(fps), threshold, isDistData=True)
    return _assign_groups([list(c) for c in clusters], len(smiles), frac, seed)


def time_split(dates: list, frac=(0.8, 0.1, 0.1), smiles: list[str] | None = None):
    """Oldest to train, newest to test. Requires a date for every compound. If smiles are given,
    compounds sharing an InChIKey connectivity block (duplicates, stereoisomers) are kept together,
    placed by their earliest date, so they never straddle partitions."""
    frac = _validate_frac(frac)
    if any(d is None for d in dates):
        raise ValueError("time split requires dates for all compounds")
    n = len(dates)
    if smiles is not None:
        if len(smiles) != n:
            raise ValueError("smiles and dates must have the same length")
        mols = _check_parseable(smiles)
        by_key: dict[str, list[int]] = {}
        for i, m in enumerate(mols):
            by_key.setdefault(inchi.MolToInchiKey(m).split("-")[0], []).append(i)
        groups = list(by_key.values())
    else:
        groups = [[i] for i in range(n)]
    groups.sort(key=lambda g: (min(dates[i] for i in g), g[0]))
    bounds = (int(round(frac[0] * n)), int(round((frac[0] + frac[1]) * n)))
    parts: list[list[int]] = [[], [], []]
    p, count = 0, 0
    for gi, g in enumerate(groups):
        remaining = len(groups) - gi
        while p < 2:
            need_later = sum(1 for q in range(p + 1, 3) if frac[q] > 0)
            skip = frac[p] == 0
            full = bool(parts[p]) and count + len(g) > bounds[p]
            if skip or full or remaining <= need_later and parts[p]:
                p += 1
            else:
                break
        parts[p] += g
        count += len(g)
    out = tuple(sorted(x) for x in parts)
    _check_nonempty(out, frac)
    return out


def split(method: str, smiles: list[str], dates: list | None = None, **kw):
    if method == "scaffold":
        return scaffold_split(smiles, **kw)
    if method == "butina":
        return butina_split(smiles, **kw)
    if method == "time":
        if dates is None:
            raise ValueError("time split needs dates")
        return time_split(dates, smiles=smiles, **kw)
    raise ValueError(f"unsupported split method {method!r}; allowed: {SPLIT_METHODS}")
