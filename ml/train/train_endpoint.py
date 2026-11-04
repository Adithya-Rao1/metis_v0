"""Training CLI for one endpoint:  python -m ml.train.train_endpoint --config ml/train/configs/<endpoint>.yaml

Hard rules enforced here (tests prove them):
  * every dataset in the config must pass ml.data.manifest.assert_trainable (license gate) BEFORE any
    data is read; otherwise exit code 2 and nothing is written.
  * deterministic: member seeds = base_seed*1000 + k; splits seeded; resumable via per-member checkpoints.
Artifacts go to ml/registry/artifacts/<endpoint>/<version>/ (git-ignored). Wave 2: never run on real data.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

import numpy as np
import yaml

from ml.data.manifest import MANIFEST_PATH, LicenseError, assert_trainable
from ml.data.splits import split as make_split
from ml.train import bundle as B
from ml.train.ensemble import POOL_SIZE, EnsemblePool
from ml.train.member import get_member_cls
from ml.uncertainty.conformal import N_VALUES, SplitConformal, ConformalByN
from ml.uncertainty.similarity import SimilarityErrorModel, SimilarityIndex

REQUIRED = ("endpoint", "version", "datasets", "kind", "backend")
ALLOWED_SPLITS = ("scaffold", "butina")  # time split needs per-compound dates, which the local loader does not carry


def load_config(path: Path | str) -> dict:
    cfg = yaml.safe_load(Path(path).read_text())
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        raise ValueError(f"config missing {missing}")
    if not cfg["datasets"] or not all(isinstance(d, str) for d in cfg["datasets"]):
        raise ValueError("config.datasets must be a non-empty list of dataset ids")
    if cfg.get("split", "scaffold") not in ALLOWED_SPLITS:
        raise ValueError(f"split must be one of {ALLOWED_SPLITS}; random splits are not offered")
    return cfg


def gate(cfg: dict, manifest_path=MANIFEST_PATH) -> list:
    """License gate. Raises LicenseError unless every dataset is `cleared`."""
    return [assert_trainable(d, manifest_path) for d in cfg["datasets"]]


def default_loader(cfg: dict) -> list[tuple[str, float, str]]:
    from ml.data.loader import load_local
    rows = []
    for d in cfg["datasets"]:
        rows += [(s, y, d) for s, y in load_local(d, cfg.get("data_cache_dir", "data"))]
    return rows


def run_training(cfg: dict, *, manifest_path=MANIFEST_PATH, loader: Callable[[dict], list] = default_loader,
                 artifacts_root: Path | str = B.ARTIFACTS_ROOT, member_cls=None, resume: bool = True,
                 on_member=None) -> Path:
    entries = gate(cfg, manifest_path)  # FIRST: nothing below runs for non-cleared data
    kind = cfg["kind"]
    for e in entries:
        if e.kind != kind:
            raise ValueError(f"dataset {e.id} is {e.kind}, config says {kind}")
    rows = loader(cfg)
    seen, smiles, y, ds_of = set(), [], [], []
    for s, v, d in rows:  # first occurrence wins (data is expected pre-cleaned by ml.data.clean)
        if s not in seen:
            seen.add(s); smiles.append(s); y.append(float(v)); ds_of.append(d)
    y = np.asarray(y)
    seed = int(cfg.get("seed", 0))
    frac = tuple(cfg.get("split_frac", (0.7, 0.15, 0.15)))
    tr, ca, te = make_split(cfg.get("split", "scaffold"), smiles, frac=frac, seed=seed)
    cls = member_cls or get_member_cls(cfg["backend"])
    root = Path(artifacts_root) / cfg["endpoint"] / cfg["version"]
    pool = EnsemblePool(cls, kind, cfg.get("member_params", {}), cfg.get("n_members", POOL_SIZE), seed)
    tr_s = [smiles[i] for i in tr]
    pool.train(tr_s, y[tr], root / "members", resume=resume, on_member=on_member)

    cal_s = [smiles[i] for i in ca]
    idx = SimilarityIndex(tr_s)
    sim_cal = idx.max_similarity(cal_s)
    P = pool.predict_members(cal_s)
    conformal = class_cal = None
    err_models = {}
    if kind == "regression":
        conformal = ConformalByN.fit(P, y[ca], sim_cal, seed=seed)
        err_models = {N: c.err_model for N, c in conformal.by_n.items()}
    else:
        class_cal = B.ClassCalibrators.fit(P, y[ca])
        err_models = {N: SimilarityErrorModel.fit(sim_cal, np.abs(y[ca] - P[:N].mean(0))) for N in N_VALUES}
    B.save_calibration(root, conformal, class_cal, err_models, {"n_cal": len(ca)})
    (root / "train_data.json").write_text(json.dumps({"smiles": tr_s, "y": [float(v) for v in y[tr]]}))
    meta = {"endpoint": cfg["endpoint"], "version": cfg["version"], "kind": kind, "backend": cfg["backend"],
            "member_params": cfg.get("member_params", {}), "n_members": pool.n_members, "base_seed": seed,
            "member_seeds": [seed * 1000 + k for k in range(pool.n_members)],
            "split": cfg.get("split", "scaffold"), "split_sizes": [len(tr), len(ca), len(te)],
            "split_indices": {"train": [int(i) for i in tr], "cal": [int(i) for i in ca], "test": [int(i) for i in te]},
            "datasets": [{"id": e.id, "sha256": B.dataset_sha256([s for s, d in zip(smiles, ds_of) if d == e.id],
                                                                  [v for v, d in zip(y, ds_of) if d == e.id]),
                          "n": sum(1 for d in ds_of if d == e.id),
                          # snapshot of the cleared manifest entry; publish re-derives and compares (F-104)
                          "name": e.name, "source_url": e.source_url, "license_as_stated": e.license_as_stated,
                          "kind": e.kind} for e in entries],
            "dataset_of_row": ds_of,
            "units": entries[0].units, "default_range": [float(y[tr].min()), float(y[tr].max())],
            "code_commit": B.code_commit(), "weights_sha256": B.weights_sha256(root / "members"),
            "all_smiles": smiles, "all_y": [float(v) for v in y]}
    (root / "meta.json").write_text(json.dumps(meta))
    return root


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Train one Metis endpoint (license-gated; owner runs this, not CI)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--artifacts-dir", default=str(B.ARTIFACTS_ROOT))
    ap.add_argument("--no-resume", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    try:
        gate(cfg)
    except (LicenseError, KeyError) as e:
        print(f"REFUSED (license gate): {e}", file=sys.stderr)
        return 2
    root = run_training(cfg, artifacts_root=a.artifacts_dir, resume=not a.no_resume,
                        on_member=lambda k, reused: print(f"member {k:02d} {'reused' if reused else 'trained'}"))
    print(f"wrote {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
