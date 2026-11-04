"""A tiny SYNTHETIC published model registry for the browser end-to-end tests.

Everything here is synthetic: random-forest members trained on generated molecules with generated labels
(tests/ml/ml_synth.py). The endpoints are named `synreg` / `syncls` and the datasets "Synthetic ...". These are not
real predictions and must never be described as such.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT / "tests" / "ml") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests" / "ml"))

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


def build_registry(root: pathlib.Path) -> pathlib.Path:
    """Train and publish `synreg` and `syncls` under `root`; returns the registry root."""
    root = pathlib.Path(root)
    manifest = root / "datasets.yaml"
    manifest.write_text(MANIFEST)
    mols = make_molecules(200, seed=1)
    for kind, ds, ep, y in (
        ("regression", "SynthReg", "synreg", regression_labels(mols)),
        ("classification", "SynthCls", "syncls", classification_labels(mols)),
    ):
        cfg = {
            "endpoint": ep,
            "version": "v1",
            "datasets": [ds],
            "kind": kind,
            "backend": "sklearn",
            "member_params": FAST,
            "seed": 1,
            "n_members": 20,
        }
        bundle = TE.run_training(
            cfg,
            manifest_path=manifest,
            loader=lambda c, y=y, ds=ds: [(s, float(v), ds) for s, v in zip(mols, y, strict=True)],
            artifacts_root=root / "artifacts",
        )
        report = run_benchmark(
            mols,
            y,
            kind,
            split_method="scaffold",
            member_cls=SklearnMember,
            member_params=FAST,
            endpoint=ep,
            seed=1,
            frac=(0.7, 0.15, 0.15),
            rf_trees=10,
            run_cv=False,
        )
        publish(bundle, report, registry_root=root, manifest_path=manifest)
    return root
