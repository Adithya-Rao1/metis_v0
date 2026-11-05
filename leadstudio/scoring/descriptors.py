"""RDKit descriptor computation -> schema.Descriptors (+ alert hits)."""

from __future__ import annotations

import threading
from collections import OrderedDict

from rdkit import Chem
from rdkit.Chem import QED, Crippen, rdMolDescriptors
from rdkit.Chem import Descriptors as D

from leadstudio.schema import AlertHit, Descriptors
from leadstudio.scoring.alerts import find_alerts


def _load_sascorer():
    """Ertl SA scorer from the repo copy (leadstudio/sa, integrity-checked data file), else unavailable."""
    try:
        from leadstudio.sa import sa_score

        sa_score.calculateScore(Chem.MolFromSmiles("CCO"))  # loads + verifies the data file
        return sa_score.calculateScore, "repo:leadstudio.sa"
    except Exception:
        return None, "unavailable"


_SA_FN, SA_SOURCE = _load_sascorer()  # SA_SOURCE reports which scorer/data file is in use


def compute_descriptors(mol: Chem.Mol) -> tuple[Descriptors, list[AlertHit]]:
    alerts = find_alerts(mol)
    mw = D.MolWt(mol)  # type: ignore[attr-defined]  # rdkit stubs omit this C++-backed name
    logp = Crippen.MolLogP(mol)  # type: ignore[attr-defined]  # rdkit stubs omit this C++-backed name
    hbd = rdMolDescriptors.CalcNumHBD(mol)
    hba = rdMolDescriptors.CalcNumHBA(mol)
    lip = int(mw > 500) + int(logp > 5) + int(hbd > 5) + int(hba > 10)
    sa: float | None = None
    if _SA_FN is not None:
        try:
            sa = float(_SA_FN(mol))
        except Exception:
            sa = None
    desc = Descriptors(
        qed=float(QED.qed(mol)),
        sa=sa,
        logp=float(logp),
        tpsa=float(rdMolDescriptors.CalcTPSA(mol)),
        mw=float(mw),
        hbd=hbd,
        hba=hba,
        rotb=rdMolDescriptors.CalcNumRotatableBonds(mol),
        fsp3=float(rdMolDescriptors.CalcFractionCSP3(mol)),
        heavy_atoms=mol.GetNumHeavyAtoms(),
        lipinski_violations=lip,
        alerts=len(alerts),
    )
    return desc, alerts


class LRU:
    """Minimal thread-safe LRU."""

    def __init__(self, maxsize: int):
        self.maxsize = maxsize
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                self.hits += 1
                return self._d[key]
            self.misses += 1
            return None

    def put(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)

    def __len__(self):
        return len(self._d)
