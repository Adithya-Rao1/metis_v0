"""Fragment-masking attribution (spec 6.3). APPROXIMATE, research-grade.

Fixed fragmentation: BRICS bond cleavage (single fragment if BRICS finds no bond). For each fragment the
fragment's atoms are deleted (valences re-sanitised, remaining pieces kept as a multi-component SMILES) and
the ensemble is re-queried; effect_i = |f(x) - f(x masked_i)|, share_i = effect_i / sum(effect). Ranges come
from resampling ensemble members (seeded). Limits: deleting atoms creates off-distribution molecules; shares
describe the model's sensitivity, not chemistry or causality; fragments interact; only one-at-a-time masking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
from rdkit import Chem, RDLogger
from rdkit.Chem import BRICS

from backend.schemas.interfaces import FragmentShare

RDLogger.DisableLog("rdApp.*")

NOTE = (
    "Approximate, research-grade. Shares are the model's sensitivity to deleting each BRICS fragment "
    "(one at a time), normalised to sum to 1. Masked molecules are off-distribution, so this is not "
    "a causal or chemical explanation. Ranges are 5th-95th percentile over ensemble member resamples."
)


def fragment_atoms(mol: Chem.Mol) -> list[list[int]]:
    """BRICS fragments as sorted atom-index lists (indices refer to `mol`). Deterministic order:
    by smallest atom index."""
    bonds = [b[0] for b in BRICS.FindBRICSBonds(mol)]
    if not bonds:
        return [list(range(mol.GetNumAtoms()))]
    cut = Chem.RWMol(mol)
    for i, j in bonds:
        cut.RemoveBond(i, j)
    frags = Chem.GetMolFrags(cut, asMols=False, sanitizeFrags=False)
    return sorted((sorted(f) for f in frags), key=lambda f: f[0])


def mask_fragment(mol: Chem.Mol, atoms: list[int]) -> str | None:
    """SMILES with `atoms` deleted, or None if nothing remains / sanitisation fails."""
    keep = set(range(mol.GetNumAtoms())) - set(atoms)
    if not keep:
        return None
    rw = Chem.RWMol(mol)
    for idx in sorted(atoms, reverse=True):
        rw.RemoveAtom(idx)
    try:
        m = rw.GetMol()
        for a in m.GetAtoms():
            a.SetNoImplicit(False)
            a.SetNumExplicitHs(0)
        Chem.SanitizeMol(m)
        return Chem.MolToSmiles(m)
    except Exception:
        return None


def train_frequency(mol: Chem.Mol, atoms: list[int], train_mols: list[Chem.Mol]) -> float:
    """Fraction of training molecules containing the fragment as a substructure."""
    if not train_mols:
        return 0.0
    try:
        smarts = Chem.MolFragmentToSmiles(mol, atomsToUse=atoms, isomericSmiles=False)
        patt = Chem.MolFromSmarts(smarts)
    except Exception:
        return 0.0
    if patt is None:
        return 0.0
    return float(sum(1 for t in train_mols if t.HasSubstructMatch(patt)) / len(train_mols))


@dataclass
class AttributionResult:
    fragments: list[FragmentShare]
    note: str = NOTE
    status: str = "approximate / research-grade"
    n_masked_failed: int = 0
    degenerate: bool = False  # all effects zero -> uniform shares


def attribute(
    smiles: str,
    predict_members: Callable[[list[str]], np.ndarray],
    train_smiles: list[str] | None = None,
    seed: int = 0,
    n_resamples: int = 100,
    lo_q: float = 5,
    hi_q: float = 95,
) -> AttributionResult:
    """predict_members(list_of_smiles) -> array (M, len(list)) of member predictions (probabilities for
    classification). Deterministic for a fixed seed."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"unparseable SMILES {smiles!r}")
    frags = fragment_atoms(mol)
    masked = [mask_fragment(mol, f) for f in frags]
    ok = [i for i, s in enumerate(masked) if s is not None]
    queries = [smiles] + [masked[i] for i in ok]
    P = np.asarray(predict_members(queries), float)  # (M, 1+len(ok))
    M = P.shape[0]

    def effects(rows: np.ndarray) -> np.ndarray:
        base = P[rows, 0].mean()
        eff = np.zeros(len(frags))
        for k, i in enumerate(ok):
            eff[i] = abs(base - P[rows, 1 + k].mean())
        return eff

    def to_shares(eff: np.ndarray) -> tuple[np.ndarray, bool]:
        t = eff.sum()
        return (eff / t, False) if t > 1e-12 else (np.full(len(eff), 1.0 / len(eff)), True)

    point, degenerate = to_shares(effects(np.arange(M)))
    rng = np.random.default_rng(seed)
    boots = np.array([to_shares(effects(rng.integers(0, M, M)))[0] for _ in range(n_resamples)])
    lo, hi = np.percentile(boots, lo_q, axis=0), np.percentile(boots, hi_q, axis=0)
    train_mols = [m for m in (Chem.MolFromSmiles(s) for s in (train_smiles or [])) if m is not None]
    out = []
    for i, f in enumerate(frags):
        c = lambda x: float(min(max(x, 0.0), 1.0))
        out.append(
            FragmentShare(
                atom_indices=f,
                share=c(point[i]),
                share_low=c(min(lo[i], point[i])),
                share_high=c(max(hi[i], point[i])),
                train_frequency=train_frequency(mol, f, train_mols),
            )
        )
    return AttributionResult(out, n_masked_failed=len(frags) - len(ok), degenerate=degenerate)
