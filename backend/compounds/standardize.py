"""Structure parsing and standardization (RDKit only; no backend imports so the sandbox worker can load it).

Pipeline per record: parse (sanitize) -> largest fragment -> cleanup/neutralize -> canonical SMILES.
Error messages are generic constants: they never contain structure content.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from rdkit import Chem, RDLogger
from rdkit.Chem import AllChem, rdDepictor
from rdkit.Chem.Draw import rdMolDraw2D
from rdkit.Chem.MolStandardize import rdMolStandardize

RDLogger.DisableLog("rdApp.*")

MAX_HEAVY_ATOMS = 60
MAX_SMILES_CHARS = 2000
MAX_MOLBLOCK_CHARS = 150_000
MAX_LABEL_CHARS = 200
MAX_CSV_FIELD = 5000
ALLOWED_Z = frozenset({1, 5, 6, 7, 8, 9, 14, 15, 16, 17, 34, 35, 53})  # H B C N O F Si P S Cl Br I Se
CONFORMER_METHOD = "RDKit ETKDGv3 embedding + MMFF94 optimization (seed 7; UFF if MMFF lacks parameters)"
EMBED_SEED = 7

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_RECORD_SPLIT = re.compile(r"^\$\$\$\$[ \t]*\r?$", re.M)

E_PARSE = "could not be parsed as a valid structure"
E_EMPTY = "has no heavy atoms"
E_BIG = "is too large (more than 60 heavy atoms)"
E_ELEMENT = "contains an unsupported element"
E_TEXT = "contains binary or control characters"
E_LONG = "is too long"
E_WS = "must be a single token without whitespace"
E_FORMAT = "is not in the expected format"


class TooManyRecords(ValueError):
    pass


class InputFormatError(ValueError):
    """Whole-input problem (never echoes input content)."""


def clean_label(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = _CTRL.sub(" ", raw).strip()[:MAX_LABEL_CHARS]
    return s or None


def standardize_mol(mol: Chem.Mol) -> tuple[str | None, str | None]:
    """Return (canonical_smiles, error). Largest fragment, cleanup, neutralize, canonical."""
    try:
        frags = Chem.GetMolFrags(mol, asMols=True, sanitizeFrags=True)
        if not frags:
            return None, E_EMPTY
        mol = max(frags, key=lambda m: (m.GetNumHeavyAtoms(), m.GetNumAtoms(), Chem.MolToSmiles(m)))
        if mol.GetNumHeavyAtoms() == 0:
            return None, E_EMPTY
        if mol.GetNumHeavyAtoms() > MAX_HEAVY_ATOMS:
            return None, E_BIG
        try:
            mol = rdMolStandardize.Cleanup(mol)
        except Exception:  # noqa: BLE001 - cleanup is a refinement; keep the sanitized molecule
            mol = Chem.Mol(mol)
        mol = rdMolStandardize.Uncharger().uncharge(mol)
        Chem.SanitizeMol(mol)
        if any(a.GetAtomicNum() not in ALLOWED_Z for a in mol.GetAtoms()):
            return None, E_ELEMENT
        if mol.GetNumHeavyAtoms() == 0:
            return None, E_EMPTY
        smi = Chem.MolToSmiles(mol)
        if not 1 <= len(smi) <= MAX_SMILES_CHARS or not smi.isascii() or re.search(r"\s", smi):
            return None, E_LONG
        return smi, None
    except Exception:  # noqa: BLE001
        return None, E_PARSE


def standardize_smiles(raw: Any) -> tuple[str | None, str | None]:
    if not isinstance(raw, str):
        return None, E_FORMAT
    s = raw.strip()
    if "\x00" in s or any(ord(c) < 32 and c not in "\t\r\n " for c in s):
        return None, E_TEXT
    if not s:
        return None, E_PARSE
    if len(s) > MAX_SMILES_CHARS:
        return None, E_LONG
    if re.search(r"\s", s):
        return None, E_WS
    params = Chem.SmilesParserParams()
    params.sanitize = True
    params.allowCXSMILES = False
    mol = Chem.MolFromSmiles(s, params)
    if mol is None:
        return None, E_PARSE
    if mol.GetNumAtoms() > 4 * MAX_HEAVY_ATOMS:
        return None, E_BIG
    return standardize_mol(mol)


def standardize_molblock(block: str) -> tuple[str | None, str | None]:
    if "\x00" in block:
        return None, E_TEXT
    if len(block) > MAX_MOLBLOCK_CHARS:
        return None, E_LONG
    if "M  END" not in block:
        return None, E_FORMAT
    mol = Chem.MolFromMolBlock(block.replace("\r\n", "\n"), sanitize=True, removeHs=True)
    if mol is None:
        return None, E_PARSE
    return standardize_mol(mol)


def _result(smi: str | None, err: str | None, label: str | None) -> dict[str, Any]:
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "canonical": smi, "label": clean_label(label)}


def records_from_csv(text: str, max_records: int) -> list[dict[str, Any]]:
    if "\x00" in text:
        raise InputFormatError("binary content is not accepted")
    csv.field_size_limit(MAX_CSV_FIELD)
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error:
        raise InputFormatError("CSV could not be parsed") from None
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise InputFormatError("CSV is empty")
    header = [c.strip().lower() for c in rows[0]]
    if "smiles" not in header:
        raise InputFormatError("CSV needs a header row with a 'smiles' column")
    body = rows[1:]
    if len(body) > max_records:
        raise TooManyRecords
    si = header.index("smiles")
    li = next((header.index(k) for k in ("name", "label", "id") if k in header), None)
    out = []
    for r in body:
        smi = r[si] if si < len(r) else ""
        label = r[li] if li is not None and li < len(r) else None
        out.append(_result(*standardize_smiles(smi), label))
    return out


def records_from_sdf(text: str, max_records: int) -> list[dict[str, Any]]:
    if "\x00" in text:
        raise InputFormatError("binary content is not accepted")
    blocks = [b for b in _RECORD_SPLIT.split(text.replace("\r\n", "\n")) if b.strip()]
    if not blocks:
        raise InputFormatError("no records found")
    if len(blocks) > max_records:
        raise TooManyRecords
    out = []
    for b in blocks:
        title = b.lstrip("\n").split("\n", 1)[0]
        out.append(_result(*standardize_molblock(b.lstrip("\n")), title))
    return out


def standardize_request(fmt: str, smiles: list[str] | None, content: str | None, max_records: int) -> list[dict[str, Any]]:
    if fmt == "smiles":
        items = smiles or []
        if not items:
            raise InputFormatError("no SMILES provided")
        if len(items) > max_records:
            raise TooManyRecords
        return [_result(*standardize_smiles(s), None) for s in items]
    if not isinstance(content, str) or not content.strip():
        raise InputFormatError("no content provided")
    if fmt == "csv":
        return records_from_csv(content, max_records)
    if fmt == "sdf":
        return records_from_sdf(content, max_records)
    if fmt == "molblock":
        return [_result(*standardize_molblock(content.lstrip("\n")), None)]
    raise InputFormatError("unknown format")


# ---------- 3D and 2D ----------


def embed_3d(smiles: str) -> dict[str, Any]:
    """ETKDG + MMFF molblock for the standardized SMILES; heavy-atom order matches MolFromSmiles."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"ok": False}
    mh = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = EMBED_SEED
    params.maxIterations = 200
    if AllChem.EmbedMolecule(mh, params) != 0:
        params.useRandomCoords = True
        if AllChem.EmbedMolecule(mh, params) != 0:
            return {"ok": False}
    method = CONFORMER_METHOD
    try:
        if AllChem.MMFFHasAllMoleculeParams(mh):
            AllChem.MMFFOptimizeMolecule(mh, maxIters=500)
        else:
            AllChem.UFFOptimizeMolecule(mh, maxIters=500)
            method = "RDKit ETKDGv3 embedding + UFF optimization (seed 7)"
    except Exception:  # noqa: BLE001
        method = "RDKit ETKDGv3 embedding (seed 7), not force-field optimized"
    return {"ok": True, "molblock": Chem.MolToMolBlock(Chem.RemoveHs(mh)), "method": method}


def depict(smiles: str, highlight: list[int], width: int = 400, height: int = 300) -> dict[str, Any]:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"ok": False}
    rdDepictor.Compute2DCoords(mol)
    n = mol.GetNumAtoms()
    hl = sorted({i for i in highlight if 0 <= i < n})
    d = rdMolDraw2D.MolDraw2DSVG(width, height)
    opts = d.drawOptions()
    opts.addAtomIndices = False
    d.DrawMolecule(mol, highlightAtoms=hl)
    d.FinishDrawing()
    conf = mol.GetConformer()
    atoms = []
    for a in mol.GetAtoms():
        p = d.GetDrawCoords(a.GetIdx())
        pos = conf.GetAtomPosition(a.GetIdx())
        atoms.append(
            {
                "index": a.GetIdx(),
                "symbol": a.GetSymbol(),
                "x": round(p.x, 2),
                "y": round(p.y, 2),
                "x2d": round(pos.x, 4),
                "y2d": round(pos.y, 4),
            }
        )
    return {"ok": True, "svg": d.GetDrawingText(), "atoms": atoms}
