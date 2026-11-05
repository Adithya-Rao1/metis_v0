"""Upload validation and 3D helpers for the server (RDKit only, no network)."""
from __future__ import annotations

from rdkit import Chem, RDLogger

RDLogger.DisableLog("rdApp.*")

MAX_HEAVY_ATOMS = 60
MAX_SMILES_CHARS = 2000
MAX_MOLFILE_CHARS = 150_000


class InvalidMolecule(ValueError):
    pass


def _clean_text(text: str, limit: int) -> str:
    if "\x00" in text:
        raise InvalidMolecule("Binary content is not accepted (SMILES / MOL / SDF text only)")
    if len(text) > limit:
        raise InvalidMolecule("Input too long")
    return text


def parse_molecule(smiles: str | None = None, molfile: str | None = None) -> Chem.Mol:
    """Parse + sanitize + size-check. Raises InvalidMolecule with a user-facing message."""
    if smiles and smiles.strip():
        s = _clean_text(smiles.strip(), MAX_SMILES_CHARS)
        if any(c in s for c in "\r\n\t "):
            raise InvalidMolecule("SMILES must be a single token without whitespace")
        mol = Chem.MolFromSmiles(s)  # sanitizes
    elif molfile and molfile.strip():
        text = _clean_text(molfile, MAX_MOLFILE_CHARS).replace("\r\n", "\n")
        text = text.split("$$$$")[0]  # SDF: first record only
        if "M  END" not in text:
            raise InvalidMolecule("Not a MOL/SDF file (missing 'M  END')")
        mol = Chem.MolFromMolBlock(text, sanitize=True, removeHs=True)
    else:
        raise InvalidMolecule("Provide a SMILES string or a MOL/SDF file")
    if mol is None:
        raise InvalidMolecule("Could not parse molecule (RDKit sanitization failed)")
    frags = Chem.GetMolFrags(mol, asMols=True)
    if len(frags) > 1:  # keep the largest component (salts / counter-ions are dropped)
        mol = max(frags, key=lambda m: m.GetNumHeavyAtoms())
    n = mol.GetNumHeavyAtoms()
    if n == 0:
        raise InvalidMolecule("Molecule has no heavy atoms")
    if n > MAX_HEAVY_ATOMS:
        raise InvalidMolecule(f"Molecule too large ({n} heavy atoms, max {MAX_HEAVY_ATOMS})")
    return mol


def canonical(mol: Chem.Mol) -> str:
    return Chem.MolToSmiles(mol)


def molblock_3d(smiles: str) -> tuple[str | None, str]:
    """Return (molblock, provider): ETKDG + MMFF from the engine's own geometry helper; (None, ..) if it cannot embed."""
    from leadstudio.engine import geometry

    try:
        block, _ = geometry.embed_3d(smiles)
    except ValueError:
        return None, "leadstudio.engine.geometry"
    return block, "leadstudio.engine.geometry"
