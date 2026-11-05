"""Tanimoto similarity to the training set, and a monotone similarity -> expected-error model."""

from __future__ import annotations

import numpy as np
from rdkit import Chem, DataStructs, RDLogger
from rdkit.Chem import rdFingerprintGenerator
from sklearn.isotonic import IsotonicRegression

RDLogger.DisableLog("rdApp.*")
_GEN = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)


def fingerprint(smiles: str):
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"unparseable SMILES {smiles!r}")
    return _GEN.GetFingerprint(mol)


def fingerprint_matrix(smiles: list[str]) -> np.ndarray:
    X = np.zeros((len(smiles), 2048), dtype=np.uint8)
    for i, s in enumerate(smiles):
        DataStructs.ConvertToNumpyArray(fingerprint(s), X[i])
    return X


class SimilarityIndex:
    """Nearest-neighbour Tanimoto search over the training set (Morgan r=2, 2048 bits)."""

    def __init__(self, train_smiles: list[str]):
        if not train_smiles:
            raise ValueError("empty training set")
        self.smiles = list(train_smiles)
        self._fps = [fingerprint(s) for s in self.smiles]

    def similarities(self, smiles: str) -> np.ndarray:
        return np.asarray(DataStructs.BulkTanimotoSimilarity(fingerprint(smiles), self._fps))

    def max_similarity(self, smiles_list: list[str]) -> np.ndarray:
        return np.array([self.similarities(s).max() for s in smiles_list])

    def nearest(self, smiles: str, k: int = 3) -> list[tuple[int, float]]:
        sims = self.similarities(smiles)
        order = np.argsort(-sims, kind="stable")[:k]
        return [(int(i), float(sims[i])) for i in order]


class SimilarityErrorModel:
    """Expected absolute error as a non-increasing function of nearest-neighbour similarity,
    fitted by isotonic regression on calibration residuals. A small floor keeps scales positive."""

    def __init__(self, x: np.ndarray, y: np.ndarray, floor: float):
        self.x = np.asarray(x, float)
        self.y = np.asarray(y, float)
        self.floor = float(floor)

    @classmethod
    def fit(cls, sim, abs_err) -> "SimilarityErrorModel":
        sim, abs_err = np.asarray(sim, float), np.asarray(abs_err, float)
        if len(sim) < 2:
            raise ValueError("need at least 2 calibration points")
        iso = IsotonicRegression(increasing=False, out_of_bounds="clip").fit(sim, abs_err)
        floor = max(1e-6, 0.1 * float(np.median(abs_err)))
        return cls(iso.X_thresholds_, iso.y_thresholds_, floor)

    def predict(self, sim) -> np.ndarray:
        return np.maximum(np.interp(np.asarray(sim, float), self.x, self.y), self.floor)

    def to_dict(self) -> dict:
        return {"x": self.x.tolist(), "y": self.y.tolist(), "floor": self.floor}

    @classmethod
    def from_dict(cls, d: dict) -> "SimilarityErrorModel":
        return cls(np.array(d["x"]), np.array(d["y"]), d["floor"])
