"""Local-cache loader. Downloads happen only with an explicit --download flag, into git-ignored data/."""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

from ml.data.manifest import ID_RE, assert_loadable, load_manifest

DEFAULT_CACHE = Path("data")


def cache_path(dataset_id: str, cache_dir: Path | str = DEFAULT_CACHE) -> Path:
    if not isinstance(dataset_id, str) or not ID_RE.match(dataset_id):
        raise ValueError("invalid dataset id: must match [A-Za-z0-9_]+")
    base = Path(cache_dir).resolve()
    p = (base / f"{dataset_id}.csv").resolve()
    if base not in p.parents:
        raise ValueError("cache path escapes the cache directory")
    return Path(cache_dir) / f"{dataset_id}.csv"


def load_local(dataset_id: str, cache_dir: Path | str = DEFAULT_CACHE, smiles_col="Drug", label_col="Y"):
    """Read a cached CSV. Never touches the network. Refuses excluded datasets."""
    assert_loadable(dataset_id)
    p = cache_path(dataset_id, cache_dir)
    if not p.exists():
        raise FileNotFoundError(f"{p} not found; run with --download (after license review) to fetch")
    with open(p, newline="") as f:
        rows = []
        for n, r in enumerate(csv.DictReader(f), start=1):
            y = float(r[label_col])
            if not math.isfinite(y):
                raise ValueError(f"{dataset_id}: non-finite label in data row {n}")
            rows.append((r[smiles_col], y))
        return rows


def download(dataset_id: str, cache_dir: Path | str = DEFAULT_CACHE) -> Path:
    """Fetch via PyTDC (optional dependency, imported lazily). Only called from the CLI flag."""
    assert_loadable(dataset_id)
    from tdc.utils import retrieve_dataset_names  # noqa: F401  (lazy; not in requirements)
    from tdc.single_pred import ADME, Tox

    cls = Tox if dataset_id in ("hERG", "hERG_Karim", "AMES", "LD50_Zhu", "DILI") else ADME
    df = cls(name=dataset_id).get_data()
    p = cache_path(dataset_id, cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False)
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Metis dataset cache")
    ap.add_argument("dataset", nargs="?")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--download", action="store_true", help="explicitly allow network download")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)
    if a.list or not a.dataset:
        for e in load_manifest().values():
            print(f"{e.id}\t{e.license_status}\t{e.kind}")
        return 0
    if not a.download:
        print("no --download flag; nothing fetched", file=sys.stderr)
        return 1
    print(download(a.dataset, a.cache_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
