"""Benchmark protocol (spec 12). Splits: time (if dates) else Butina + scaffold; never random-only.
Baselines from ml.data.baselines (RF on Morgan FP; GSE for solubility; Crippen for logP).
Metrics: RMSE/MAE/R2 vs noise ceiling; AUROC/MCC/AUPRC; 5x2cv paired t-test; coverage by similarity bin;
reliability/ECE; runtime at each N. Writes a JSON report. Wave 2: exercised on synthetic data only."""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
from scipy import stats
from sklearn.metrics import average_precision_score, matthews_corrcoef, mean_absolute_error, r2_score, roc_auc_score

from ml.data import baselines
from ml.data.splits import SPLIT_METHODS, scaffold_split, split as make_split
from ml.train.ensemble import POOL_SIZE, EnsemblePool
from ml.uncertainty.conformal import (N_VALUES, ConformalByN, IsotonicRecalibrator, VennAbers, coverage_report,
                                      ece, reliability_data)
from ml.uncertainty.similarity import SimilarityIndex

GSE_ENDPOINTS = {"solubility"}
CRIPPEN_ENDPOINTS = {"logp"}


def default_split_methods(has_dates: bool) -> list[str]:
    return ["time"] if has_dates else ["butina", "scaffold"]


def regression_metrics(y, p) -> dict:
    y, p = np.asarray(y, float), np.asarray(p, float)
    return {"rmse": float(np.sqrt(np.mean((y - p) ** 2))), "mae": float(mean_absolute_error(y, p)), "r2": float(r2_score(y, p))}


def classification_metrics(y, p) -> dict:
    y, p = np.asarray(y, int), np.asarray(p, float)
    if len(set(y.tolist())) < 2:
        return {"auroc": None, "mcc": None, "auprc": None, "note": "test set has a single class"}
    return {"auroc": float(roc_auc_score(y, p)), "mcc": float(matthews_corrcoef(y, p >= 0.5)),
            "auprc": float(average_precision_score(y, p))}


def _loss(kind: str, y, p) -> float:
    """Primary loss for the paired test: RMSE (regression) or 1 - AUROC (classification)."""
    if kind == "regression":
        return regression_metrics(y, p)["rmse"]
    m = classification_metrics(y, p)["auroc"]
    return float("nan") if m is None else 1.0 - m


def cv5x2_paired_t(smiles, y, kind, member_cls, member_params, n_members=3, seed=0, rf_trees=50) -> dict:
    """Dietterich (1998) 5x2cv paired t-test: ensemble mean (n_members) vs RF-on-Morgan baseline on
    scaffold-grouped folds. diff = loss(model) - loss(baseline); negative favours the model."""
    y = np.asarray(y)
    diffs = []
    for r in range(5):
        a, _, b = scaffold_split(list(smiles), frac=(0.5, 0.0, 0.5), seed=seed + r)
        pair = []
        for tr, te in ((a, b), (b, a)):
            tr_s, te_s = [smiles[i] for i in tr], [smiles[i] for i in te]
            pool = EnsemblePool(member_cls, kind, member_params, n_members, seed + r).train(tr_s, y[tr])
            pm = pool.predict_members(te_s).mean(0)
            rf = baselines.rf_baseline(tr_s, y[tr], kind, n_estimators=rf_trees, seed=seed + r)
            pb = baselines.rf_predict(rf, te_s, kind)
            pair.append(_loss(kind, y[te], pm) - _loss(kind, y[te], pb))
        diffs.append(pair)
    d = np.array(diffs)
    if np.isnan(d).any():
        return {"p_value": None, "t": None, "note": "a fold had a single class; test not computed"}
    s2 = ((d - d.mean(1, keepdims=True)) ** 2).sum(1)
    denom = np.sqrt(s2.mean())
    if denom == 0:
        return {"p_value": None if d[0, 0] != 0 else 1.0, "t": None, "mean_diff": float(d.mean()), "note": "zero variance"}
    t = float(d[0, 0] / denom)
    return {"t": t, "df": 5, "p_value": float(2 * stats.t.sf(abs(t), 5)), "mean_diff": float(d.mean()),
            "loss": "rmse" if kind == "regression" else "1-auroc", "comparison": "ensemble mean vs RF(Morgan)"}


def run_benchmark(smiles, y, kind: str, *, split_method: str, member_cls, member_params=None, endpoint: str = "endpoint",
                  dates=None, n_members: int = POOL_SIZE, seed: int = 0, noise_sd: float | None = None,
                  frac=(0.6, 0.2, 0.2), rf_trees: int = 50, cv_members: int = 3, run_cv: bool = True,
                  out_path: Path | str | None = None) -> dict:
    if split_method not in SPLIT_METHODS:
        raise ValueError(f"split must be one of {SPLIT_METHODS}; random splits are not allowed")
    smiles, y = list(smiles), np.asarray(y, float)
    tr, ca, te = make_split(split_method, smiles, dates=dates, frac=frac, **({} if split_method == "time" else {"seed": seed}))
    tr_s, ca_s, te_s = ([smiles[i] for i in ix] for ix in (tr, ca, te))
    pool = EnsemblePool(member_cls, kind, member_params, n_members, seed).train(tr_s, y[tr])
    idx = SimilarityIndex(tr_s)
    sim_cal, sim_te = idx.max_similarity(ca_s), idx.max_similarity(te_s)
    Pc, Pt = pool.predict_members(ca_s), pool.predict_members(te_s)
    ns = [n for n in N_VALUES if n <= n_members]
    rep: dict = {"endpoint": endpoint, "kind": kind, "split": split_method, "split_sizes": [len(tr), len(ca), len(te)],
                 "seed": seed, "n_members": n_members, "ns": ns, "random_split_results": "never reported",
                 "ensemble_note": "metrics use the mean of all pool members"}
    ytr, yte = y[tr], y[te]
    rf = baselines.rf_baseline(tr_s, ytr, kind, n_estimators=rf_trees, seed=seed)
    base_preds = {"rf_morgan": baselines.rf_predict(rf, te_s, kind)}
    if endpoint in GSE_ENDPOINTS:
        base_preds["gse"] = np.array([baselines.gse_logs(s)[0] for s in te_s])
        rep["gse_note"] = baselines.GSE_NOTE
    if endpoint in CRIPPEN_ENDPOINTS:
        base_preds["crippen_logp"] = np.array([baselines.crippen_logp(s) for s in te_s])
    mfn = regression_metrics if kind == "regression" else classification_metrics
    ens_mean = Pt.mean(0)
    rep["metrics"] = {"ensemble": mfn(yte, ens_mean), **{k: mfn(yte, v) for k, v in base_preds.items()}}
    if kind == "regression":
        rep["noise_ceiling"] = ({"noise_sd": float(noise_sd), "rmse_ceiling": float(noise_sd),
                                 "ensemble_rmse_over_ceiling": rep["metrics"]["ensemble"]["rmse"] / float(noise_sd)}
                                if noise_sd else {"status": "unknown", "note": "assay noise ceiling not available for this endpoint"})
    rep["paired_5x2cv"] = (cv5x2_paired_t(smiles, y, kind, member_cls, member_params, cv_members, seed, rf_trees)
                           if run_cv else {"status": "not run"})
    cov, rel = {}, {}
    if kind == "regression":
        cb = ConformalByN.fit(Pc, y[ca], sim_cal, seed=seed)
        for N in ns:
            mean, lo, hi = cb.predict(Pt, sim_te, N)
            cov[str(N)] = {**coverage_report(yte, lo, hi, sim_te), "target": 1 - cb.by_n[N].alpha,
                           "median_cal_width": cb.by_n[N].median_width}
        rep["coverage_by_n"] = cov
    else:
        for N in ns:
            raw = Pt[:N].mean(0)
            sc, lab = Pc[:N].mean(0), y[ca]
            iso = IsotonicRecalibrator().fit(sc, lab).predict(raw)
            va = VennAbers().fit(sc, lab).predict(raw)
            rel[str(N)] = {"raw": reliability_data(raw, yte), "isotonic": reliability_data(iso, yte),
                           "venn_abers": reliability_data(va, yte)}
        rep["reliability_by_n"] = rel
    rt = {}
    for N in ns:
        best = min(_timed(lambda: pool.predict_members(te_s, n=N)) for _ in range(3))
        rt[str(N)] = best / len(te_s)
    rep["runtime_seconds_per_compound_by_n"] = rt
    rep["runtime_note"] = "wall-clock member inference only, this machine, batch of test compounds"
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(rep, indent=2))
    return rep


def _timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


def run_protocol(smiles, y, kind, *, has_dates=False, dates=None, out_dir: Path | str | None = None, **kw) -> dict:
    """All default splits for the data (time if dates else Butina + scaffold)."""
    reports = {}
    for m in default_split_methods(has_dates or dates is not None):
        reports[m] = run_benchmark(smiles, y, kind, split_method=m, dates=dates, **kw,
                                   out_path=(Path(out_dir) / f"{kw.get('endpoint', 'endpoint')}_{m}.json") if out_dir else None)
    return reports
