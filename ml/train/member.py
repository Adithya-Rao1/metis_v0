"""MemberModel adapter interface. Real backend: Chemprop v2 (chemprop_member.py). Tests inject SklearnMember."""

from __future__ import annotations

import joblib
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np


class MemberModel(ABC):
    """One ensemble member. predict() returns values (regression) or P(class 1) (classification)."""

    backend: str = "abstract"

    def __init__(self, kind: str, **params):
        if kind not in ("regression", "classification"):
            raise ValueError(f"bad kind {kind!r}")
        self.kind = kind
        self.params = params

    @abstractmethod
    def fit(self, smiles: list[str], y: np.ndarray, seed: int) -> "MemberModel": ...

    @abstractmethod
    def predict(self, smiles: list[str]) -> np.ndarray: ...

    @abstractmethod
    def save(self, path: Path) -> None: ...

    @classmethod
    @abstractmethod
    def load(cls, path: Path) -> "MemberModel": ...


class SklearnMember(MemberModel):
    """Fast Morgan-fingerprint random forest. Used for tests and as a cheap local backend; not the production model."""

    backend = "sklearn"

    def fit(self, smiles, y, seed):
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
        from ml.uncertainty.similarity import fingerprint_matrix

        cls = RandomForestRegressor if self.kind == "regression" else RandomForestClassifier
        self.model = cls(
            n_estimators=self.params.get("n_estimators", 30),
            random_state=seed,
            n_jobs=1,
            min_samples_leaf=self.params.get("min_samples_leaf", 1),
        )
        self.model.fit(fingerprint_matrix(list(smiles)), np.asarray(y))
        return self

    def predict(self, smiles):
        from ml.uncertainty.similarity import fingerprint_matrix

        X = fingerprint_matrix(list(smiles))
        if self.kind == "regression":
            return self.model.predict(X)
        proba = self.model.predict_proba(X)
        classes = list(self.model.classes_)
        return proba[:, classes.index(1)] if 1 in classes else np.zeros(len(X))

    def save(self, path):
        joblib.dump({"kind": self.kind, "params": self.params, "model": self.model}, path)

    @classmethod
    def load(
        cls, path
    ):  # joblib files are executable-on-load: only call this on files whose sha256 was verified against the manifest
        d = joblib.load(path)
        m = cls(d["kind"], **d["params"])
        m.model = d["model"]
        return m


def get_member_cls(backend: str):
    if backend == "sklearn":
        return SklearnMember
    if backend == "chemprop":
        from ml.train.chemprop_member import ChempropMember

        return ChempropMember
    raise ValueError(f"unknown backend {backend!r}")
