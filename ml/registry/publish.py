"""Publish a trained bundle into the registry manifest. Refuses unless the license gate passes and every
model-card field is real (measured / computed, not placeholder)."""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path

from backend.schemas.contracts import ENSEMBLE_SIZES
from backend.schemas.interfaces import ModelCard
from ml.data import cards
from ml.data.manifest import MANIFEST_PATH, assert_trainable
from ml.registry.predictor import predict_with_bundle
from ml.registry.registry import REGISTRY_ROOT, model_version_id
from ml.benchmarks.protocol import test_set_sha256
from ml.train.bundle import bundle_files_sha256, bundle_sha256, dataset_sha256, load_bundle, weights_sha256

_PLACEHOLDER = re.compile(r"placeholder|todo|tbd|lorem|xxx|fixme|n/a|not yet", re.I)


class PublishRefused(ValueError):
    pass


def _walk(x, path=""):
    if isinstance(x, dict):
        for k, v in x.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(x, (list, tuple)):
        for i, v in enumerate(x):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, x


def assert_real(card: ModelCard) -> None:
    """Every field must be real: no blank/placeholder strings, finite numbers, all four N present."""
    d = card.model_dump()
    for path, v in _walk(d):
        if isinstance(v, str) and (not v.strip() or _PLACEHOLDER.search(v)):
            if path.endswith(".units") or path == ".units":
                continue
            raise PublishRefused(f"card field {path} is blank or a placeholder")
        if isinstance(v, float) and not math.isfinite(v):
            raise PublishRefused(f"card field {path} is not finite")
    for name in ("coverage_by_n", "runtime_by_n_seconds"):
        got = d[name]
        if set(got) != {str(n) for n in ENSEMBLE_SIZES}:
            raise PublishRefused(f"{name} must cover N in {ENSEMBLE_SIZES}, got {sorted(got)}")
    if not all(0 < v <= 1 for v in d["coverage_by_n"].values()):
        raise PublishRefused("coverage_by_n values must be in (0,1]")
    if not all(v > 0 for v in d["runtime_by_n_seconds"].values()):
        raise PublishRefused("runtime_by_n_seconds must be measured (> 0)")
    lo, hi = d["default_range"]
    if not lo < hi:
        raise PublishRefused("default_range must have min < max")
    if not re.fullmatch(r"[0-9a-f]{64}", d["weights_sha256"]):
        raise PublishRefused("weights_sha256 is not a sha256")
    if not d["benchmark"].get("metrics"):
        raise PublishRefused("benchmark has no metrics")
    if d["split_type"] not in ("scaffold", "butina", "time"):
        raise PublishRefused("split_type must be scaffold, butina or time")
    if d["kind"] == "regression" and not (d["units"] or "").strip():
        raise PublishRefused("regression card requires units")


def measure_runtime(bundle, version_id, sample_smiles: list[str], repeats: int = 1) -> dict:
    """Seconds per compound at each N for the full prediction path (incl. attribution), and load overhead."""
    out = {}
    for N in ENSEMBLE_SIZES:
        t = time.perf_counter()
        for _ in range(repeats):
            for s in sample_smiles:
                predict_with_bundle(bundle, version_id, s, N)
        out[str(N)] = (time.perf_counter() - t) / (repeats * len(sample_smiles))
    return out


def build_card(meta: dict, bundle, version_id, benchmark: dict, manifest_path, runtime_by_n: dict,
               limitations: str | None = None) -> ModelCard:
    kind = meta["kind"]
    entries = {d["id"]: assert_trainable(d["id"], manifest_path) for d in meta["datasets"]}
    split_sizes = meta["split_sizes"]
    datasets = [{"name": e.name, "url": e.source_url, "license": e.license_as_stated,
                 "size": next(d["n"] for d in meta["datasets"] if d["id"] == e.id),
                 "split": f"{meta['split']} (train/cal/test = {split_sizes[0]}/{split_sizes[1]}/{split_sizes[2]})"}
                for e in entries.values()]
    widths: dict[str, float] = {}
    if kind == "regression":
        cov = {n: v["coverage"] for n, v in benchmark["coverage_by_n"].items()}
        widths = {n: float(v["median_cal_width"]) for n, v in benchmark["coverage_by_n"].items()}  # F-72: ranking confidence
    else:  # classification: 1 - ECE of the Venn-Abers probability on the held-out test set
        cov = {n: min(max(1 - v["venn_abers"]["ece"], 0.01), 0.99) for n, v in benchmark["reliability_by_n"].items()}
    bm = {k: benchmark[k] for k in ("split", "split_sizes", "metrics", "paired_5x2cv", "noise_ceiling", "gse_note",
                                    "coverage_by_n", "reliability_by_n", "runtime_seconds_per_compound_by_n") if k in benchmark}
    return ModelCard(endpoint=meta["endpoint"], version=meta["version"], kind=kind, units=meta.get("units"),
                     datasets=datasets, limitations=limitations or cards.LIMITATIONS[kind], split_type=meta["split"],
                     benchmark=bm, coverage_by_n=cov, runtime_by_n_seconds=runtime_by_n,
                     default_range=tuple(meta["default_range"]), median_calibration_width_by_n=widths, weights_sha256=meta["weights_sha256"])


def verify_datasets(meta: dict, manifest_path) -> None:
    """F-104: derive every dataset's identity from the cleared manifest by id and require meta.json to agree."""
    ds = meta.get("datasets")
    if not isinstance(ds, list) or not ds:
        raise PublishRefused("meta.json lists no datasets")
    ids = [d.get("id") for d in ds]
    if len(set(ids)) != len(ids):
        raise PublishRefused("duplicate dataset ids in meta.json")
    rows = meta.get("dataset_of_row")
    for d in ds:
        e = assert_trainable(d["id"], manifest_path)  # license gate; raises LicenseError / KeyError
        if e.kind != meta["kind"]:
            raise PublishRefused(f"dataset {e.id} is {e.kind}, bundle is {meta['kind']}")
        for k, want in (("name", e.name), ("source_url", e.source_url), ("license_as_stated", e.license_as_stated), ("kind", e.kind)):
            if d.get(k) != want:
                raise PublishRefused(f"meta.json dataset {e.id} {k}={d.get(k)!r} does not match the cleared manifest ({want!r})")
        if rows is not None:
            if len(rows) != len(meta["all_smiles"]):
                raise PublishRefused("meta.json dataset_of_row length mismatch")
            sel = [i for i, r in enumerate(rows) if r == e.id]
            if len(sel) != d.get("n") or dataset_sha256([meta["all_smiles"][i] for i in sel], [meta["all_y"][i] for i in sel]) != d.get("sha256"):
                raise PublishRefused(f"dataset {e.id}: rows in the bundle do not match its recorded n/sha256")
        else:
            raise PublishRefused("meta.json has no per-row dataset assignment (dataset_of_row); cannot verify provenance")
    if set(rows) != set(ids):
        raise PublishRefused("bundle rows reference datasets not listed in meta.json")


def verify_benchmark(meta: dict, benchmark: dict) -> None:
    """F-100: the benchmark must describe exactly this bundle's split, seed, ensemble, params and test compounds."""
    if benchmark.get("endpoint") != meta["endpoint"] or benchmark.get("split") != meta["split"] or benchmark.get("kind") != meta["kind"]:
        raise PublishRefused("benchmark report does not match this bundle (endpoint/kind/split)")
    if list(benchmark.get("split_sizes") or []) != list(meta["split_sizes"]):
        raise PublishRefused("benchmark split sizes differ from the bundle's split")
    if benchmark.get("seed") != meta["base_seed"]:
        raise PublishRefused("benchmark seed differs from the bundle's seed")
    if benchmark.get("n_members") != meta["n_members"]:
        raise PublishRefused("benchmark n_members differs from the bundle's")
    if benchmark.get("member_params") != meta.get("member_params", {}):
        raise PublishRefused("benchmark member params differ from the bundle's")
    want = test_set_sha256([meta["all_smiles"][i] for i in meta["split_indices"]["test"]])
    if benchmark.get("test_sha256") != want:
        raise PublishRefused("benchmark test set differs from the bundle's held-out test set")


def publish(bundle_dir: Path | str, benchmark: dict, registry_root: Path | str = REGISTRY_ROOT,
            manifest_path=MANIFEST_PATH, limitations: str | None = None) -> ModelCard:
    bundle_dir, registry_root = Path(bundle_dir).resolve(), Path(registry_root).resolve()
    meta = json.loads((bundle_dir / "meta.json").read_text())
    verify_datasets(meta, manifest_path)  # license gate + ids/names/licences derived from the manifest
    if registry_root / "artifacts" not in bundle_dir.parents:
        raise PublishRefused("bundle must live under <registry_root>/artifacts/")
    if meta["code_commit"] == "unknown":
        raise PublishRefused("bundle has no code commit")
    actual = weights_sha256(bundle_dir / "members")
    if actual != meta["weights_sha256"]:
        raise PublishRefused("weights hash does not match bundle meta")
    verify_benchmark(meta, benchmark)
    if benchmark.get("ns") is None or set(benchmark["ns"]) != set(ENSEMBLE_SIZES):
        raise PublishRefused("benchmark must cover N in (3,5,10,20)")
    t = time.perf_counter()
    files_sha = bundle_files_sha256(bundle_dir)
    bsha = bundle_sha256(bundle_dir)
    bundle = load_bundle(bundle_dir, bsha, expected_weights_sha256=actual)
    load_s = time.perf_counter() - t
    vid = model_version_id(meta["endpoint"], meta["version"], actual)
    test_smiles = [meta["all_smiles"][i] for i in meta["split_indices"]["test"]][:5]
    per = measure_runtime(bundle, vid, test_smiles)
    card = build_card(meta, bundle, vid, benchmark, manifest_path, per, limitations)
    assert_real(card)
    entry = {
        "version": meta["version"], "model_version_id": str(vid),
        "bundle_dir": str(bundle_dir.relative_to(registry_root)), "weights_sha256": actual,
        "bundle_sha256": bsha, "bundle_files_sha256": files_sha,
        "card": json.loads(card.model_dump_json()),
        "runtime": {"fixed_overhead_seconds": load_s, "seconds_per_compound_by_n": per,
                    "measured_on_n_compounds": len(test_smiles)},
        "provenance": {"datasets": meta["datasets"], "code_commit": meta["code_commit"], "seeds": {
            "base_seed": meta["base_seed"], "member_seeds": meta["member_seeds"]}, "backend": meta["backend"],
            "member_params": meta["member_params"],
            "member_files_sha256": {k.split("/", 1)[1]: v for k, v in files_sha.items() if k.startswith("members/")}},
        "calibration_by_n": {n: {k: v for k, v in c.items() if k != "err_model"}
                             for n, c in json.loads((bundle_dir / "calibration.json").read_text())["conformal"].items()}
        if meta["kind"] == "regression" else {
            n: {"n_cal": len(v["labels"])} for n, v in json.loads((bundle_dir / "calibration.json").read_text())["class_cal"].items()},
        "benchmark": benchmark,
    }
    mp = registry_root / "manifest.json"
    manifest = json.loads(mp.read_text()) if mp.exists() else {"schema": 1, "models": {}}
    manifest["models"][meta["endpoint"]] = entry
    tmp = mp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    os.replace(tmp, mp)
    return card
