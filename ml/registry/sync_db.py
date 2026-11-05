"""Sync published registry models into the `model_version` table (prediction.model_version_id has an FK to it).

    METIS_MIGRATION_URL=postgresql://... python -m ml.registry.sync_db [--registry-root DIR] [--manifest datasets.yaml]

Runs with the MIGRATION role DSN (metis_app can only SELECT model_version). Idempotent. The row id is the same
deterministic uuid5 the registry returns in every Prediction. A row is `published` only if the publish gate
re-passes here (real card, every dataset cleared in the licence manifest, id/hash consistent); otherwise it is
written as `draft` with licenses_cleared=false. A published row is never modified (the DB trigger forbids it);
a conflicting row (same endpoint/version, different id or weights) is an error."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from backend.schemas.interfaces import ModelCard
from ml.data.manifest import MANIFEST_PATH, assert_trainable
from ml.registry.publish import PublishRefused, assert_real
from ml.registry.registry import REGISTRY_ROOT, model_version_id


class SyncConflict(RuntimeError):
    pass


def gate_passed(entry: dict, endpoint: str, manifest_path=MANIFEST_PATH) -> tuple[bool, str]:
    try:
        card = ModelCard(**entry["card"])
        assert_real(card)
        if card.endpoint != endpoint:
            raise PublishRefused("card endpoint differs from manifest key")
        if (
            str(model_version_id(endpoint, card.version, entry["weights_sha256"]))
            != entry["model_version_id"]
        ):
            raise PublishRefused("model_version_id is not the endpoint+version+weights uuid5")
        if card.weights_sha256 != entry["weights_sha256"]:
            raise PublishRefused("card weights hash differs from entry")
        if not entry.get("bundle_sha256"):
            raise PublishRefused("entry has no bundle hash")
        for d in entry["provenance"]["datasets"]:
            assert_trainable(d["id"], manifest_path)
        return True, "ok"
    except Exception as e:  # noqa: BLE001 - any failure means "not published"
        return False, f"{type(e).__name__}: {e}"


def rows_from_registry(root: Path | str = REGISTRY_ROOT, manifest_path=MANIFEST_PATH) -> list[dict]:
    mp = Path(root) / "manifest.json"
    models = json.loads(mp.read_text())["models"] if mp.exists() else {}
    rows = []
    for endpoint, e in sorted(models.items()):
        ok, why = gate_passed(e, endpoint, manifest_path)
        c = e["card"]
        rows.append(
            {
                "id": e["model_version_id"],
                "endpoint": endpoint,
                "version": c["version"],
                "status": "published" if ok else "draft",
                "datasets": c["datasets"],
                "licenses_cleared": ok,
                "limitations": c["limitations"],
                "split_type": c["split_type"],
                "benchmark": c["benchmark"],
                "coverage_by_n": c["coverage_by_n"],
                "weights_sha256": e["weights_sha256"],
                "_why": why,
            }
        )
    return rows


_INSERT = """INSERT INTO model_version (id, endpoint, version, status, datasets, licenses_cleared, limitations, split_type,
                           benchmark, coverage_by_n, weights_sha256)
VALUES (%(id)s, %(endpoint)s, %(version)s, %(status)s, %(datasets)s, %(licenses_cleared)s, %(limitations)s,
        %(split_type)s, %(benchmark)s, %(coverage_by_n)s, %(weights_sha256)s)"""


def sync(dsn: str, root: Path | str = REGISTRY_ROOT, manifest_path=MANIFEST_PATH) -> dict[str, str]:
    """Upsert one row per registry model; returns {endpoint: resulting status}."""
    import psycopg
    from psycopg.types.json import Jsonb

    out = {}
    with psycopg.connect(dsn) as conn:
        for r in rows_from_registry(root, manifest_path):
            why = r.pop("_why")
            cur = conn.execute(
                "SELECT id::text, status, weights_sha256 FROM model_version WHERE endpoint=%s AND version=%s",
                (r["endpoint"], r["version"]),
            ).fetchone()
            p = {
                **r,
                "datasets": Jsonb(r["datasets"]),
                "benchmark": Jsonb(r["benchmark"]),
                "coverage_by_n": Jsonb(r["coverage_by_n"]),
            }
            if cur is None:
                conn.execute(_INSERT, p)
                out[r["endpoint"]] = r["status"]
            elif cur[0] != r["id"] or cur[2] != r["weights_sha256"]:
                raise SyncConflict(
                    f"{r['endpoint']} {r['version']}: DB row {cur[0]} has different weights than the registry"
                )
            elif cur[1] != "draft":
                out[r["endpoint"]] = cur[1]  # published/retired rows are immutable here
            else:
                conn.execute(
                    "UPDATE model_version SET status=%(status)s, datasets=%(datasets)s, licenses_cleared=%(licenses_cleared)s, "
                    "limitations=%(limitations)s, split_type=%(split_type)s, benchmark=%(benchmark)s, "
                    "coverage_by_n=%(coverage_by_n)s WHERE id=%(id)s",
                    p,
                )
                out[r["endpoint"]] = r["status"]
            if r["status"] == "draft":
                print(f"NOT published {r['endpoint']}: {why}", file=sys.stderr)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Upsert model_version rows from the registry manifest (migration role)"
    )
    ap.add_argument("--registry-root", default=str(REGISTRY_ROOT))
    ap.add_argument("--manifest", default=str(MANIFEST_PATH), help="dataset licence manifest (datasets.yaml)")
    a = ap.parse_args(argv)
    dsn = os.environ.get("METIS_MIGRATION_URL")
    if not dsn:
        print("METIS_MIGRATION_URL (migration-role DSN) is required", file=sys.stderr)
        return 2
    try:
        res = sync(dsn, a.registry_root, a.manifest)
    except SyncConflict as e:
        print(f"CONFLICT: {e}", file=sys.stderr)
        return 3
    for k, v in res.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
