"""License-gated dataset manifest. Training requires license_status == cleared."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

MANIFEST_PATH = Path(__file__).with_name("datasets.yaml")
STATUSES = {"cleared", "pending", "excluded"}
KINDS = {"regression", "classification"}
ALWAYS_REFUSED = {"FreeSolv"}
ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
# A cleared entry may never look like FreeSolv (any id) or carry non-commercial / no-derivatives terms.
_EXCLUDED_SOURCE_RE = re.compile(r"free[\W_]*solv|mobleylab", re.I)
# Exact allow-list of NORMALISED permissive licence identifiers (see normalise_licence). Anything else is not cleared.
ALLOWED_LICENCES = frozenset(
    {"cc0", "cc0-1.0", "cc-by-4.0", "cc-by-sa-4.0", "mit", "apache-2.0", "bsd-3-clause", "public-domain"}
)
_JUNK_CONFIRMERS = {"", "false", "true", "none", "null", "nan", "n/a", "na", "unknown", "x", "tbd", "todo"}


class LicenseError(ValueError):
    """Licence-gate violation. Subclasses ValueError so malformed-manifest handling stays uniform."""


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
        e = DatasetEntry(
            **{k: (str(r[k]) if r[k] is not None else None) for k in DatasetEntry.__dataclass_fields__}
        )
        if e.license_status not in STATUSES:
            raise ValueError(f"{e.id}: bad license_status {e.license_status}")
        if e.kind not in KINDS:
            raise ValueError(f"{e.id}: bad kind {e.kind}")
        if not ID_RE.match(e.id):
            raise ValueError(f"bad dataset id {e.id!r}: must match [A-Za-z0-9_]+")
        if e.license_status == "cleared":
            _validate_cleared(e)
        if e.id in out:
            raise ValueError(f"duplicate id {e.id}")
        out[e.id] = e
    return out


def normalise_licence(text: str) -> str:
    """Unicode NFKC + case folding, surrounding space stripped, runs of whitespace/underscores -> '-'.
    'CC BY 4.0' -> 'cc-by-4.0'. The result must EQUAL an allow-list entry; no substring/regex matching."""
    t = unicodedata.normalize("NFKC", text or "").casefold().strip()
    return re.sub(r"[\s_]+", "-", t)


def _validate_cleared(e: DatasetEntry) -> None:
    if e.id in ALWAYS_REFUSED or any(
        _EXCLUDED_SOURCE_RE.search(x or "") for x in (e.id, e.name, e.source_url)
    ):
        raise LicenseError(f"{e.id}: FreeSolv (or an alias/source of it) can never be cleared")
    lic = e.license_as_stated or ""
    if normalise_licence(lic) not in ALLOWED_LICENCES:
        raise LicenseError(
            f"{e.id}: licence {lic!r} is not on the permissive allow-list {sorted(ALLOWED_LICENCES)}; cannot be cleared"
        )
    try:
        d = date.fromisoformat((e.date_checked or "").strip())
    except ValueError:
        raise ValueError(f"{e.id}: cleared requires date_checked as an ISO date (YYYY-MM-DD)") from None
    if d > date.today():
        raise ValueError(f"{e.id}: date_checked is in the future")
    who = (e.confirmed_by or "").strip()
    if who.lower() in _JUNK_CONFIRMERS or not re.search(r"[A-Za-z]", who):
        raise ValueError(f"{e.id}: cleared requires a real confirmed_by")


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
