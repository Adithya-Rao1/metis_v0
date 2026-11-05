"""3D geometry helpers: ETKDG + MMFF embedding, MCS alignment, atom maps.

Atom indices always refer to ``Chem.MolFromSmiles(smiles)`` of the SMILES passed in
(heavy atoms only; hydrogens are removed after embedding, which preserves heavy-atom order).
"""

from __future__ import annotations

import time

from rdkit import Chem
from rdkit.Chem import AllChem, rdFMCS

EMBED_SEED = 7
MCS_TIMEOUT_S = 2
EMBED_BUDGET_S = (
    3.0  # wall-clock cap for all ETKDG attempts of one molecule (RDKit checks it inside the search)
)
EMBED_MIN_RETRY_S = 0.5

MolLike = str | Chem.Mol


def _as_mol(m: MolLike) -> Chem.Mol | None:
    return Chem.MolFromSmiles(m) if isinstance(m, str) else m


def embed_raw(smiles: str, seed: int = EMBED_SEED) -> Chem.Mol | None:
    """ETKDG + MMFF (UFF fallback) embedding. Returns a heavy-atom mol with one 3D conformer, or None."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    mh = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()  # type: ignore[attr-defined]  # rdkit stubs omit this C++-backed name
    params.randomSeed = seed
    params.maxIterations = 200
    deadline = time.monotonic() + EMBED_BUDGET_S
    params.timeout = max(1, int(EMBED_BUDGET_S * 0.8))
    if AllChem.EmbedMolecule(mh, params) != 0:  # type: ignore[attr-defined]  # rdkit stubs omit this C++-backed name
        left = deadline - time.monotonic()
        if left < EMBED_MIN_RETRY_S:
            return None
        params.useRandomCoords = True
        params.timeout = max(1, int(left))
        if AllChem.EmbedMolecule(mh, params) != 0:  # type: ignore[attr-defined]  # rdkit stubs omit this C++-backed name
            return None
    try:
        if AllChem.MMFFHasAllMoleculeParams(mh):  # type: ignore[attr-defined]  # rdkit stubs omit this C++-backed name
            AllChem.MMFFOptimizeMolecule(mh, maxIters=200)  # type: ignore[attr-defined]  # rdkit stubs omit this C++-backed name
        else:
            AllChem.UFFOptimizeMolecule(mh, maxIters=200)  # type: ignore[attr-defined]  # rdkit stubs omit this C++-backed name
    except Exception:  # noqa: S110  # nosec B110
        pass
    return Chem.RemoveHs(mh)


def mcs_match(parent: MolLike, child: MolLike) -> list[tuple[int, int]]:
    """Partial (parent_idx, child_idx) map over the maximum common substructure."""
    p, c = _as_mol(parent), _as_mol(child)
    if p is None or c is None:
        return []
    res = rdFMCS.FindMCS(
        [p, c],
        timeout=MCS_TIMEOUT_S,
        ringMatchesRingOnly=True,
        atomCompare=rdFMCS.AtomCompare.CompareElements,
        bondCompare=rdFMCS.BondCompare.CompareOrder,
    )
    if res.numAtoms == 0:
        return []
    patt = Chem.MolFromSmarts(res.smartsString)
    a, b = p.GetSubstructMatch(patt), c.GetSubstructMatch(patt)
    return list(zip(a, b)) if a and b else []


def atom_map(parent: MolLike, child: MolLike) -> list[tuple[int, int]]:
    """Schema ``Candidate.atom_map``: (parent idx, child idx) pairs over the MCS."""
    return mcs_match(parent, child)


def align_to(child: Chem.Mol, ref: Chem.Mol, amap: list[tuple[int, int]]) -> None:
    """Rigidly align ``child`` onto ``ref`` in place using the (parent, child) atom map."""
    if len(amap) >= 3:
        AllChem.AlignMol(child, ref, atomMap=[(c, p) for p, c in amap])  # type: ignore[attr-defined]  # rdkit stubs omit this C++-backed name


def embed_3d(smiles: str, ref_mol: Chem.Mol | None = None) -> tuple[str, Chem.Mol]:
    """(molblock, mol): 3D heavy-atom embedding, rigid-aligned on the MCS to ``ref_mol`` when given.

    Raises ValueError if the molecule cannot be embedded.
    """
    mol = embed_raw(smiles)
    if mol is None:
        raise ValueError(f"3D embedding failed for {smiles}")
    if ref_mol is not None:
        align_to(mol, ref_mol, mcs_match(ref_mol, mol))
    return Chem.MolToMolBlock(mol), mol


def _flat_smiles(m: Chem.Mol) -> str:
    return Chem.MolToSmiles(m, isomericSmiles=False)


def same_graph(a: Chem.Mol, b: Chem.Mol) -> bool:
    """True if the two heavy-atom molecules have the same atoms (elements, charges) in the same order, joined by the
    same bonds. Bond orders are compared through the canonical SMILES (stereo ignored: a 3D block carries stereo the seed SMILES may
    leave open) so aromatic and Kekule forms agree."""
    if a.GetNumAtoms() != b.GetNumAtoms() or a.GetNumBonds() != b.GetNumBonds():
        return False
    if any(
        x.GetAtomicNum() != y.GetAtomicNum() or x.GetFormalCharge() != y.GetFormalCharge()
        for x, y in zip(a.GetAtoms(), b.GetAtoms())
    ):
        return False
    pairs = lambda m: {tuple(sorted((x.GetBeginAtomIdx(), x.GetEndAtomIdx()))) for x in m.GetBonds()}  # noqa: E731
    return pairs(a) == pairs(b) and _flat_smiles(a) == _flat_smiles(b)


def seed_from_block(smiles: str, block: str) -> tuple[str, Chem.Mol] | None:
    """(molblock, mol) of the seed taken from a client-supplied 3D mol block, or None if the block is not usable.

    Usable means: parses and sanitizes, has a 3D conformer, and its heavy-atom graph is the seed's graph. The result is
    re-written in the atom order of ``MolFromSmiles(smiles)`` (what every other index in a run refers to), so a block
    with another atom order is accepted and a different molecule never is. Never raises."""
    try:
        want = Chem.MolFromSmiles(smiles)
        got = Chem.MolFromMolBlock(block, sanitize=True, removeHs=True)
        if want is None or got is None or got.GetNumConformers() != 1:
            return None
        conf = got.GetConformer()
        if not conf.Is3D() and all(abs(conf.GetAtomPosition(i).z) < 1e-6 for i in range(got.GetNumAtoms())):
            return None  # flat (2D) coordinates are not a 3D seed
        if _flat_smiles(want) != _flat_smiles(got):
            return None
        match = got.GetSubstructMatch(want, useChirality=False)
        if len(match) != want.GetNumAtoms() or not same_graph(want, Chem.RenumberAtoms(got, list(match))):
            return None
        out = Chem.RenumberAtoms(got, list(match))
        return Chem.MolToMolBlock(out), out
    except Exception:
        return None
