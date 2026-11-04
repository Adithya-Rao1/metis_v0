"""RDKit cleaning: standardize, dedupe by InChIKey, drop contradictory labels."""
from __future__ import annotations

from dataclasses import dataclass, asdict

import math

import numpy as np
from rdkit import Chem, RDLogger
from rdkit.Chem import inchi
from rdkit.Chem.MolStandardize import rdMolStandardize

RDLogger.DisableLog("rdApp.*")


@dataclass
class CleaningReport:
    n_input: int = 0
    n_unparseable: int = 0
    n_non_finite_dropped: int = 0
    n_empty_after_standardize: int = 0
    n_merged_duplicates: int = 0
    n_contradictory_dropped: int = 0  # contradictory compounds (groups)
    n_contradictory_records: int = 0  # input records inside those groups
    n_output: int = 0

    def reconciles(self) -> bool:
        removed = (self.n_unparseable + self.n_non_finite_dropped + self.n_empty_after_standardize
                   + self.n_contradictory_records + self.n_merged_duplicates)
        return self.n_input == removed + self.n_output

    def as_dict(self) -> dict:
        return asdict(self)


def standardize(smiles: str, keep_stereo: bool = True) -> tuple[str, str] | None:
    """Return (canonical_smiles, inchikey) or None. Largest fragment, neutralized."""
    if not isinstance(smiles, str) or not smiles.strip():
        return None
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    mol = rdMolStandardize.LargestFragmentChooser().choose(mol)
    mol = rdMolStandardize.Uncharger().uncharge(mol)
    if mol.GetNumAtoms() == 0:
        return None
    if not keep_stereo:
        Chem.RemoveStereochemistry(mol)
    Chem.SanitizeMol(mol)
    smi = Chem.MolToSmiles(mol, isomericSmiles=keep_stereo)
    key = inchi.MolToInchiKey(mol)
    if not key:
        return None
    return smi, key


def clean_dataset(
    records: list[tuple[str, float]],
    kind: str,
    tolerance: float = 0.5,
    keep_stereo: bool = True,
) -> tuple[list[dict], CleaningReport]:
    """records: (smiles, label). Regression: duplicates whose label range > tolerance are dropped,
    otherwise merged by mean. Classification: any disagreement drops the compound."""
    if kind not in ("regression", "classification"):
        raise ValueError("kind must be regression or classification")
    rep = CleaningReport(n_input=len(records))
    groups: dict[str, dict] = {}
    for smi, y in records:
        try:
            yf = float(y)
        except (TypeError, ValueError):
            yf = float("nan")
        if not math.isfinite(yf):
            rep.n_non_finite_dropped += 1
            continue
        y = yf
        s = standardize(smi, keep_stereo)
        if s is None:
            if isinstance(smi, str) and smi.strip() and Chem.MolFromSmiles(smi) is not None:
                rep.n_empty_after_standardize += 1
            else:
                rep.n_unparseable += 1
            continue
        csmi, key = s
        g = groups.setdefault(key, {"smiles": csmi, "labels": []})
        g["labels"].append(float(y))
    out = []
    for key, g in groups.items():
        ys = np.array(g["labels"])
        if len(ys) > 1:
            if kind == "classification":
                bad = len(set(ys.tolist())) > 1
            else:
                bad = float(ys.max() - ys.min()) > tolerance
            if bad:
                rep.n_contradictory_dropped += 1
                rep.n_contradictory_records += len(ys)
                continue
            rep.n_merged_duplicates += len(ys) - 1
        y = float(ys[0]) if kind == "classification" else float(ys.mean())
        out.append({"smiles": g["smiles"], "inchikey": key, "label": y, "n_measurements": len(ys)})
    rep.n_output = len(out)
    if not rep.reconciles():
        raise RuntimeError(f"cleaning report does not reconcile: {rep}")
    return out, rep
