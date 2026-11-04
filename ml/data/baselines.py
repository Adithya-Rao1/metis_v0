"""Baselines: RF on Morgan FP, General Solubility Equation, Crippen logP."""
from __future__ import annotations

import numpy as np
from rdkit import Chem, DataStructs
from rdkit.Chem import AllChem, Crippen
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor

GSE_NOTE = ("General Solubility Equation: logS = 0.5 - 0.01*(MP-25) - logP. Melting point is usually not "
            "available; when missing, the (MP-25) term is dropped (assumes a liquid or low-melting solid), "
            "so logS is an upper-bound approximation for crystalline solids. Crippen logP is used for logP.")


def morgan_matrix(smiles: list[str], radius: int = 2, n_bits: int = 2048) -> np.ndarray:
    X = np.zeros((len(smiles), n_bits), dtype=np.uint8)
    for i, s in enumerate(smiles):
        fp = AllChem.GetMorganFingerprintAsBitVect(Chem.MolFromSmiles(s), radius, n_bits)
        arr = np.zeros((n_bits,), dtype=np.uint8)
        DataStructs.ConvertToNumpyArray(fp, arr)
        X[i] = arr
    return X


def rf_baseline(train_smiles, y_train, kind: str, n_estimators: int = 200, seed: int = 0):
    cls = RandomForestRegressor if kind == "regression" else RandomForestClassifier
    model = cls(n_estimators=n_estimators, random_state=seed, n_jobs=1)
    model.fit(morgan_matrix(list(train_smiles)), y_train)
    return model


def rf_predict(model, smiles, kind: str) -> np.ndarray:
    X = morgan_matrix(list(smiles))
    if kind == "regression":
        return model.predict(X)
    proba = model.predict_proba(X)
    classes = list(model.classes_)
    if 1 in classes:
        return proba[:, classes.index(1)]
    return np.zeros(len(X))  # single-class training set without a positive class


def crippen_logp(smiles: str) -> float:
    return float(Crippen.MolLogP(Chem.MolFromSmiles(smiles)))


def gse_logs(smiles: str, melting_point_c: float | None = None) -> tuple[float, bool]:
    """Returns (logS in log mol/L, mp_was_used)."""
    logp = crippen_logp(smiles)
    if melting_point_c is None:
        return 0.5 - logp, False
    return 0.5 - 0.01 * (melting_point_c - 25.0) - logp, True
