"""Split conformal regression (per ensemble size N) with similarity-scaled widths, and
classification recalibration (isotonic, Venn-Abers) with ECE / reliability data.

Regression: for each N in {3,5,10,20} the point prediction is the mean of the first N pool members;
the calibration set (scaffold-split, disjoint from training) gives normalised scores
|y - mean| / sigma(sim), where sigma(sim) is the expected absolute error at that nearest-neighbour
Tanimoto similarity. Scores use cross-fitted sigma so sigma never sees the point it scores.
Interval = mean +/- q * sigma(sim), q = finite-sample-corrected (1 - alpha) quantile.
"""
from __future__ import annotations

import math

import numpy as np
from sklearn.isotonic import IsotonicRegression

from ml.uncertainty.similarity import SimilarityErrorModel

N_VALUES = (3, 5, 10, 20)
TARGET_COVERAGE = 0.9
SIM_BIN_EDGES = (0.0, 0.3, 0.5, 0.7, 1.0000001)


def _conformal_q(scores: np.ndarray, alpha: float) -> float:
    n = len(scores)
    k = math.ceil((n + 1) * (1 - alpha))
    if k > n:
        return float("inf")
    return float(np.sort(scores)[k - 1])


class SplitConformal:
    """One calibrated regressor for a fixed N. scaled=False gives plain constant-width split conformal."""

    def __init__(self, q: float, err_model: SimilarityErrorModel | None, alpha: float, n_cal: int, median_width: float):
        self.q, self.err_model, self.alpha, self.n_cal, self.median_width = q, err_model, alpha, n_cal, median_width

    @classmethod
    def fit(cls, pred, y, sim, alpha: float = 1 - TARGET_COVERAGE, scaled: bool = True,
            n_folds: int = 5, seed: int = 0) -> "SplitConformal":
        pred, y, sim = (np.asarray(a, float) for a in (pred, y, sim))
        n = len(y)
        if n < 10:
            raise ValueError("calibration set too small (need >= 10)")
        err = np.abs(y - pred)
        if not scaled:
            q = _conformal_q(err, alpha)
            return cls(q, None, alpha, n, 2 * q)
        folds = np.random.default_rng(seed).permutation(n) % min(n_folds, n)
        scores = np.empty(n)
        for f in range(folds.max() + 1):
            te = folds == f
            m = SimilarityErrorModel.fit(sim[~te], err[~te])
            scores[te] = err[te] / m.predict(sim[te])
        model = SimilarityErrorModel.fit(sim, err)
        q = _conformal_q(scores, alpha)
        widths = 2 * q * model.predict(sim)
        return cls(q, model, alpha, n, float(np.median(widths)))

    def half_width(self, sim) -> np.ndarray:
        sim = np.asarray(sim, float)
        if self.err_model is None:
            return np.full(sim.shape, self.q)
        return self.q * self.err_model.predict(sim)

    def interval(self, pred, sim):
        pred = np.asarray(pred, float)
        h = self.half_width(sim)
        return pred - h, pred + h

    def to_dict(self) -> dict:
        return {"q": self.q, "alpha": self.alpha, "n_cal": self.n_cal, "median_width": self.median_width,
                "err_model": self.err_model.to_dict() if self.err_model else None}

    @classmethod
    def from_dict(cls, d: dict) -> "SplitConformal":
        em = SimilarityErrorModel.from_dict(d["err_model"]) if d["err_model"] else None
        return cls(d["q"], em, d["alpha"], d["n_cal"], d["median_width"])


class ConformalByN:
    """Calibration computed separately for each N using the first N pool members."""

    def __init__(self, by_n: dict[int, SplitConformal]):
        self.by_n = by_n

    @classmethod
    def fit(cls, member_preds, y, sim, alpha: float = 1 - TARGET_COVERAGE, ns=N_VALUES, seed: int = 0,
            scaled: bool = True) -> "ConformalByN":
        member_preds = np.asarray(member_preds, float)  # (M, n_cal)
        out = {}
        for N in ns:
            if N > member_preds.shape[0]:
                raise ValueError(f"need >= {N} members to calibrate N={N}, have {member_preds.shape[0]}")
            out[N] = SplitConformal.fit(member_preds[:N].mean(0), y, sim, alpha, scaled=scaled, seed=seed)
        return cls(out)

    def predict(self, member_preds, sim, N: int):
        """member_preds: (M, n). Returns (mean, low, high)."""
        mean = np.asarray(member_preds, float)[:N].mean(0)
        lo, hi = self.by_n[N].interval(mean, sim)
        return mean, lo, hi

    def to_dict(self) -> dict:
        return {str(N): c.to_dict() for N, c in self.by_n.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "ConformalByN":
        return cls({int(N): SplitConformal.from_dict(v) for N, v in d.items()})


def coverage_report(y, low, high, sim, edges=SIM_BIN_EDGES) -> dict:
    """Empirical coverage and mean width, overall and per similarity bin."""
    y, low, high, sim = (np.asarray(a, float) for a in (y, low, high, sim))
    hit = (y >= low) & (y <= high)
    bins = []
    for lo_e, hi_e in zip(edges[:-1], edges[1:]):
        m = (sim >= lo_e) & (sim < hi_e)
        bins.append({"sim_low": lo_e, "sim_high": min(hi_e, 1.0), "n": int(m.sum()),
                     "coverage": float(hit[m].mean()) if m.any() else None,
                     "mean_width": float((high - low)[m].mean()) if m.any() else None})
    return {"n": int(len(y)), "coverage": float(hit.mean()), "mean_width": float((high - low).mean()), "by_similarity_bin": bins}


# ---------------------------------------------------------------- classification

def reliability_data(prob, y, n_bins: int = 10) -> dict:
    """Reliability-diagram data: per equal-width bin, count, mean predicted probability, observed fraction positive."""
    prob, y = np.asarray(prob, float), np.asarray(y, float)
    idx = np.minimum((prob * n_bins).astype(int), n_bins - 1)
    bins = []
    for b in range(n_bins):
        m = idx == b
        bins.append({"lo": b / n_bins, "hi": (b + 1) / n_bins, "count": int(m.sum()),
                     "mean_pred": float(prob[m].mean()) if m.any() else None,
                     "frac_pos": float(y[m].mean()) if m.any() else None})
    return {"bins": bins, "ece": ece(prob, y, n_bins), "n": int(len(prob))}


def ece(prob, y, n_bins: int = 10) -> float:
    """Expected calibration error, equal-width bins."""
    prob, y = np.asarray(prob, float), np.asarray(y, float)
    idx = np.minimum((prob * n_bins).astype(int), n_bins - 1)
    total = 0.0
    for b in range(n_bins):
        m = idx == b
        if m.any():
            total += m.sum() * abs(prob[m].mean() - y[m].mean())
    return float(total / len(prob))


class IsotonicRecalibrator:
    def __init__(self):
        self.iso = None

    def fit(self, scores, labels):
        self.iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(scores, labels)
        return self

    def predict(self, scores) -> np.ndarray:
        return np.clip(self.iso.predict(np.asarray(scores, float)), 0, 1)


class VennAbers:
    """Inductive Venn-Abers predictor (Vovk & Petej). Returns the merged probability
    p = p1 / (1 - p0 + p1) and the (p0, p1) multiprobability interval."""

    def __init__(self):
        self.scores = self.labels = None

    def fit(self, scores, labels):
        self.scores, self.labels = np.asarray(scores, float), np.asarray(labels, float)
        return self

    def predict_interval(self, scores) -> tuple[np.ndarray, np.ndarray]:
        scores = np.asarray(scores, float)
        p0, p1 = np.empty(len(scores)), np.empty(len(scores))
        for i, s in enumerate(scores):
            xs = np.append(self.scores, s)
            for lab, out in ((0.0, p0), (1.0, p1)):
                ys = np.append(self.labels, lab)
                iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(xs, ys)
                out[i] = float(iso.predict([s])[0])
        return p0, p1

    def predict(self, scores) -> np.ndarray:
        p0, p1 = self.predict_interval(scores)
        return np.clip(p1 / (1.0 - p0 + p1), 0, 1)
