"""License-gated dataset manifest. Training requires license_status == cleared."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

MANIFEST_PATH = Path(__file__).with_name("datasets.yaml")
STATUSES = {"cleared", "pending", "excluded"}
KINDS = {"regression", "classification"}
ALWAYS_REFUSED = {"FreeSolv"}


class LicenseError(Exception):
    pass


@dataclass(frozen=True)
class DatasetEntry:
    id: str
    name: str
    source_url: str
    endpoint: str
    kind: str
    units: str
    license_as_stated: str
    license_status: str
    date_checked: str | None
    confirmed_by: str | None
    notes: str


def load_manifest(path: Path | str = MANIFEST_PATH) -> dict[str, DatasetEntry]:
    raw = yaml.safe_load(Path(path).read_text())["datasets"]
    out: dict[str, DatasetEntry] = {}
    for r in raw:
        e = DatasetEntry(**{k: (str(r[k]) if r[k] is not None else None) for k in DatasetEntry.__dataclass_fields__})
        if e.license_status not in STATUSES:
            raise ValueError(f"{e.id}: bad license_status {e.license_status}")
        if e.kind not in KINDS:
            raise ValueError(f"{e.id}: bad kind {e.kind}")
        if e.license_status == "cleared" and not (e.date_checked and e.confirmed_by):
            raise ValueError(f"{e.id}: cleared requires date_checked and confirmed_by")
        if e.id in out:
            raise ValueError(f"duplicate id {e.id}")
        out[e.id] = e
    return out


def get(dataset_id: str, path: Path | str = MANIFEST_PATH) -> DatasetEntry:
    m = load_manifest(path)
    if dataset_id not in m:
        raise KeyError(f"unknown dataset {dataset_id}")
    return m[dataset_id]


def assert_trainable(dataset_id: str, path: Path | str = MANIFEST_PATH) -> DatasetEntry:
    e = get(dataset_id, path)
    if dataset_id in ALWAYS_REFUSED or e.license_status != "cleared":
        raise LicenseError(f"{dataset_id} is not trainable: license_status={e.license_status}")
    return e


def assert_loadable(dataset_id: str, path: Path | str = MANIFEST_PATH) -> DatasetEntry:
    """Loading for cleaning/inspection is allowed for pending data, never for excluded/FreeSolv."""
    e = get(dataset_id, path)
    if dataset_id in ALWAYS_REFUSED or e.license_status == "excluded":
        raise LicenseError(f"{dataset_id} is excluded and may never be loaded")
    return e
