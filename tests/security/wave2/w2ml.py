"""Builds a tiny synthetic published registry (RF members, synthetic molecules) for the ML red-team checks."""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
for p in (ROOT, ROOT / "tests" / "ml"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ml_synth import classification_labels, make_molecules, regression_labels  # noqa: E402

from ml.benchmarks.protocol import run_benchmark  # noqa: E402
from ml.registry.publish import publish  # noqa: E402
from ml.train import train_endpoint as TE  # noqa: E402
from ml.train.member import SklearnMember  # noqa: E402

FAST = {"n_estimators": 6}
MANIFEST = """datasets:
  - id: SynthReg
    name: Synthetic regression set
    source_url: https://example.org/synth-reg
    endpoint: synthetic logP
    kind: regression
    units: log units
    license_as_stated: "CC BY 4.0"
    license_status: cleared
    date_checked: "2026-01-02"
    confirmed_by: "Test Owner"
    notes: "generated for tests"
  - id: SynthCls
    name: Synthetic classification set
    source_url: https://example.org/synth-cls
    endpoint: synthetic class
    kind: classification
    units: "binary"
    license_as_stated: "CC0"
    license_status: cleared
    date_checked: "2026-01-02"
    confirmed_by: "Test Owner"
    notes: "generated for tests"
"""


def data(n=200):
    mols = make_molecules(n, seed=1)
    return mols, regression_labels(mols), classification_labels(mols)


def train(root, manifest, kind, ds, endpoint, mols, y, version="v1"):
    cfg = {"endpoint": endpoint, "version": version, "datasets": [ds], "kind": kind, "backend": "sklearn",
           "member_params": FAST, "seed": 1, "n_members": 20}
    return TE.run_training(cfg, manifest_path=manifest, loader=lambda c: [(s, float(v), ds) for s, v in zip(mols, y)],
                           artifacts_root=pathlib.Path(root) / "artifacts")


def bench(kind, mols, y, endpoint, frac=(0.7, 0.15, 0.15)):
    return run_benchmark(mols, y, kind, split_method="scaffold", member_cls=SklearnMember, member_params=FAST,
                         endpoint=endpoint, seed=1, frac=frac, rf_trees=10, run_cv=False)


def build_registry(root, manifest):
    mols, yreg, ycls = data()
    out = {}
    for kind, ds, ep, y in (("regression", "SynthReg", "synreg", yreg), ("classification", "SynthCls", "syncls", ycls)):
        b = train(root, manifest, kind, ds, ep, mols, y)
        rep = bench(kind, mols, y, ep)
        card = publish(b, rep, registry_root=root, manifest_path=manifest)
        out[ep] = (b, rep, card)
    out["mols"], out["yreg"], out["ycls"] = mols, yreg, ycls
    return out
