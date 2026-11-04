"""Turns a loaded Bundle into a PredictionDetail (spec 5/6). Shared by the registry and by publish-time timing."""
from __future__ import annotations

import time
from uuid import UUID

import numpy as np

from backend.schemas.contracts import Prediction
from backend.schemas.interfaces import PredictionDetail
from ml.train.bundle import Bundle
from ml.uncertainty.attribution import attribute
from ml.uncertainty.conformal import N_VALUES, TARGET_COVERAGE
from ml.uncertainty.decompose import decompose

REG_NOTE = ("Mean of the first {n} members of a 20-member Chemprop-style ensemble. Interval: split conformal on a "
            "scaffold-split calibration set (n={ncal}), width scaled by nearest-neighbour Tanimoto similarity to the "
            "training set (max similarity {sim:.2f}); nominal coverage {cov:.0%}. Assay noise is not estimated. "
            "Fragment shares are approximate, research-grade.")
CLS_NOTE = ("Mean of the first {n} members, recalibrated with Venn-Abers on a scaffold-split calibration set "
            "(n={ncal}). The coverage field is 1 - ECE of the recalibrated probability on a held-out test set, "
            "not an interval coverage. Nearest-neighbour similarity to training: {sim:.2f}. Assay noise is not "
            "estimated. Fragment shares are approximate, research-grade.")


def predict_with_bundle(b: Bundle, version_id: UUID, canonical_smiles: str, N: int, coverage_hint: float | None = None,
                        with_fragments: bool = True) -> PredictionDetail:
    if N not in N_VALUES:
        raise ValueError(f"ensemble_n must be one of {N_VALUES}")
    t0 = time.perf_counter()
    meta = b.meta
    P = b.pool.predict_members([canonical_smiles], n=N)  # (N, 1)
    sims = b.index.similarities(canonical_smiles)
    sim = float(sims.max())
    sigma = float(b.err_models[N].predict(sim))
    dec = decompose(P[:, 0], sigma, noise_sd=None, seed=0)
    n_cal = meta.get("n_cal") or 0
    if b.kind == "regression":
        mean, lo, hi = b.conformal.predict(P, np.array([sim]), N)
        cal = b.conformal.by_n[N]
        pred = Prediction(endpoint=meta["endpoint"], kind="regression", model_version_id=version_id, ensemble_n=N,
                          value=float(mean[0]), interval_low=float(lo[0]), interval_high=float(hi[0]),
                          coverage=1 - cal.alpha,  # nominal; empirical test coverage is on the model card
                          sample_size=len(b.train_smiles), breakdown=dec.to_breakdown(),
                          method_note=REG_NOTE.format(n=N, ncal=cal.n_cal, sim=sim, cov=1 - cal.alpha))
    else:
        raw = float(P.mean(0)[0])
        p = float(b.class_cal.venn_abers(N).predict([raw])[0])
        pred = Prediction(endpoint=meta["endpoint"], kind="classification", model_version_id=version_id, ensemble_n=N,
                          value=raw, probability=p, coverage=TARGET_COVERAGE if coverage_hint is None else coverage_hint,
                          sample_size=len(b.train_smiles), breakdown=dec.to_breakdown(),
                          method_note=CLS_NOTE.format(n=N, ncal=len(b.class_cal.per_n[N]["labels"]), sim=sim))
    frags = []
    if with_fragments:
        frags = attribute(canonical_smiles, lambda ss: b.pool.predict_members(list(ss), n=N), b.train_smiles, seed=0).fragments
    nn = [{"smiles": b.train_smiles[i], "similarity": s, "label": b.train_y[i]} for i, s in b.index.nearest(canonical_smiles, 3)]
    return PredictionDetail(prediction=pred, fragments=frags, nearest_training=nn, runtime_seconds=time.perf_counter() - t0)
