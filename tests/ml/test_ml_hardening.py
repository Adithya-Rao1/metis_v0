"""Wave 2 ML hardening: F-79 (model_version sync), F-100 (benchmark identity), F-101/F-102 (whole-bundle hash,
mandatory on load), F-103 (licence allow-list), F-104 (dataset provenance). Synthetic data only."""
import json
import pathlib
import shutil

import joblib
import pytest

from ml.benchmarks.protocol import run_benchmark
from ml.data.manifest import LicenseError, load_manifest, normalise_licence
from ml.registry.publish import PublishRefused, publish
from ml.registry.registry import FileModelRegistry, model_version_id
from ml.train import train_endpoint as TE
from ml.train.bundle import bundle_sha256, load_bundle
from ml.train.member import SklearnMember

FAST = {"n_estimators": 6}


@pytest.fixture(scope="module")
def reg(tmp_path_factory, cleared_manifest, mols, yreg, ycls):
    root = tmp_path_factory.mktemp("hreg")
    for kind, ds, ep, y in (("regression", "SynthReg", "synreg", yreg), ("classification", "SynthCls", "syncls", ycls)):
        cfg = {"endpoint": ep, "version": "v1", "datasets": [ds], "kind": kind, "backend": "sklearn",
               "member_params": FAST, "seed": 1, "n_members": 20}
        b = TE.run_training(cfg, manifest_path=cleared_manifest, loader=lambda c, ds=ds, y=y: [(s, float(v), ds) for s, v in zip(mols, y)],
                            artifacts_root=root / "artifacts")
        publish(b, _bench(kind, mols, y, ep), registry_root=root, manifest_path=cleared_manifest)
    return root


def _bench(kind, mols, y, ep, frac=(0.7, 0.15, 0.15), seed=1):
    return run_benchmark(mols, y, kind, split_method="scaffold", member_cls=SklearnMember, member_params=FAST, endpoint=ep,
                         seed=seed, frac=frac, rf_trees=10, run_cv=False)


def _clone(reg, tmp_path):
    d = tmp_path / "r"
    shutil.copytree(reg, d)
    return d


# ---- F-100
@pytest.mark.parametrize("kw", [{"frac": (0.6, 0.2, 0.2)}, {"seed": 2}])
def test_f100_publish_rejects_other_split_or_seed(reg, tmp_path, cleared_manifest, mols, yreg, kw):
    d = _clone(reg, tmp_path)
    with pytest.raises(PublishRefused):
        publish(d / "artifacts/synreg/v1", _bench("regression", mols, yreg, "synreg", **kw), registry_root=d, manifest_path=cleared_manifest)


@pytest.mark.parametrize("field,val", [("test_sha256", "0" * 64), ("member_params", {"n_estimators": 7}), ("n_members", 10)])
def test_f100_publish_rejects_edited_identity(reg, tmp_path, cleared_manifest, mols, yreg, field, val):
    d = _clone(reg, tmp_path)
    rep = {**_bench("regression", mols, yreg, "synreg"), field: val}
    with pytest.raises(PublishRefused):
        publish(d / "artifacts/synreg/v1", rep, registry_root=d, manifest_path=cleared_manifest)


# ---- F-101 / F-102
@pytest.mark.parametrize("fname", ["calibration.json", "train_data.json", "meta.json"])
def test_f101_every_served_file_is_hashed(reg, tmp_path, fname):
    d = _clone(reg, tmp_path)
    f = d / "artifacts/synreg/v1" / fname
    f.write_text(f.read_text() + " ")
    with pytest.raises(ValueError, match="hash"):
        FileModelRegistry(d).predict("synreg", "CCO", 5)


def test_f101_extra_file_and_calibration_scaling_detected(reg, tmp_path):
    d = _clone(reg, tmp_path)
    b = d / "artifacts/synreg/v1"
    cal = json.loads((b / "calibration.json").read_text())
    for n in cal["conformal"]:
        cal["conformal"][n]["q"] *= 0.01
    (b / "calibration.json").write_text(json.dumps(cal))
    with pytest.raises(ValueError):
        FileModelRegistry(d).predict("synreg", "CCO", 5)
    d2 = _clone(reg, tmp_path / "x")
    (d2 / "artifacts/synreg/v1/members/evil.ckpt").write_bytes(b"x")
    with pytest.raises(ValueError):
        FileModelRegistry(d2).predict("synreg", "CCO", 5)


def test_f101_untampered_still_serves(reg):
    assert FileModelRegistry(reg).predict("synreg", "CCO", 5).prediction.endpoint == "synreg"


def test_f102_load_bundle_needs_verified_hash_and_never_unpickles_unverified(reg, tmp_path):
    d = _clone(reg, tmp_path)
    b = d / "artifacts/synreg/v1"
    marker = tmp_path / "pwned"

    class E:
        def __reduce__(self):
            return (pathlib.Path(str(marker)).write_text, ("x",))

    joblib.dump(E(), b / "members" / "member_00.ckpt")
    for args in ((), (None,), ("",), ("0" * 64,)):
        with pytest.raises((TypeError, ValueError)):
            load_bundle(b, *args)
    with pytest.raises(TypeError):
        load_bundle(b, expected_weights_sha256="0" * 64)  # a members-only hash is not accepted any more
    assert not marker.exists()
    # even the correct hash of the poisoned directory is only reachable by whoever controls the out-of-band record
    assert bundle_sha256(b) != json.loads((d / "manifest.json").read_text())["models"]["synreg"]["bundle_sha256"]


# ---- F-103
@pytest.mark.parametrize("lic", ["CC BY 4.0", "cc0", "CC0-1.0", "cc by-sa 4.0", "MIT", "Apache-2.0", "BSD-3-Clause", "Public Domain",
                                 "ＣＣ ＢＹ ４.０"])
def test_f103_allow_list_accepts(tmp_path, lic):
    assert _lic(tmp_path, lic)["d1"].license_status == "cleared"


@pytest.mark.parametrize("lic", ["CC BY 4.0 NoCommercial", "CC BY 4.0 (not for commercial use)", "CC BY-ＮＣ 4.0", "CC BY-NC 4.0",
                                 "CC BY-ND 4.0", "CC BY 3.0", "GPL-3.0", "CC BY 4.0 or custom", "", "Not Specified", "mit license"])
def test_f103_everything_else_not_cleared(tmp_path, lic):
    with pytest.raises(LicenseError):
        _lic(tmp_path, lic)


def test_f103_normaliser():
    assert normalise_licence("  CC BY 4.0 ") == "cc-by-4.0"


def _lic(tmp_path, lic):
    p = tmp_path / "m.yaml"
    p.write_text('datasets:\n - id: d1\n   name: n\n   source_url: https://x.org\n   endpoint: e\n   kind: regression\n   units: u\n'
                 f'   license_as_stated: "{lic}"\n   license_status: cleared\n   date_checked: "2026-01-02"\n   confirmed_by: "Owner"\n   notes: n\n')
    return load_manifest(p)


# ---- F-104
def _publish_edited(reg, tmp_path, manifest, mols, yreg, edit):
    d = _clone(reg, tmp_path)
    b = d / "artifacts/synreg/v1"
    meta = json.loads((b / "meta.json").read_text())
    edit(meta)
    (b / "meta.json").write_text(json.dumps(meta))
    (d / "manifest.json").unlink()
    return publish(b, _bench("regression", mols, yreg, "synreg"), registry_root=d, manifest_path=manifest)


@pytest.mark.parametrize("edit", [
    lambda m: m["datasets"][0].update(id="SynthCls"),
    lambda m: m["datasets"][0].update(name="Other set"),
    lambda m: m["datasets"][0].update(license_as_stated="MIT"),
    lambda m: m["datasets"][0].update(sha256="0" * 64),
    lambda m: m["datasets"][0].update(n=5),
    lambda m: m["datasets"].append(dict(m["datasets"][0])),
    lambda m: m.update(datasets=[]),
])
def test_f104_publish_rejects_meta_dataset_tampering(reg, tmp_path, cleared_manifest, mols, yreg, edit):
    with pytest.raises((PublishRefused, LicenseError, KeyError)):
        _publish_edited(reg, tmp_path, cleared_manifest, mols, yreg, edit)


def test_f104_unedited_meta_republishes(reg, tmp_path, cleared_manifest, mols, yreg):
    card = _publish_edited(reg, tmp_path, cleared_manifest, mols, yreg, lambda m: None)
    assert card.datasets[0]["name"] == "Synthetic regression set"


# ---- F-79
@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver")
    import psycopg
    srv = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        for m in sorted((pathlib.Path(__file__).parents[2] / "backend/db/migrations").glob("*.sql")):
            c.execute(m.read_text())
    yield uri
    srv.cleanup()


def test_f79_sync_creates_published_rows_matching_prediction_ids(pg, reg, cleared_manifest):
    import psycopg

    from ml.registry import sync_db

    res = sync_db.sync(pg, reg, cleared_manifest)
    assert res == {"syncls": "published", "synreg": "published"}
    assert sync_db.sync(pg, reg, cleared_manifest) == res  # idempotent
    r = FileModelRegistry(reg)
    with psycopg.connect(pg) as c:
        for ep in ("synreg", "syncls"):
            pred = r.predict(ep, "CCOc1ccccc1", 5).prediction
            row = c.execute("SELECT status, licenses_cleared, weights_sha256 FROM model_version WHERE id=%s",
                            (pred.model_version_id,)).fetchone()
            assert row == ("published", True, r.model_card(ep).weights_sha256)
            assert pred.model_version_id == model_version_id(ep, "v1", row[2])


def test_f79_not_published_when_gate_fails_and_conflict_detected(pg, reg, tmp_path, cleared_manifest):
    import psycopg

    from ml.registry import sync_db

    pending = tmp_path / "p.yaml"
    pending.write_text(cleared_manifest.read_text().replace("license_status: cleared", "license_status: pending"))
    d = _clone(reg, tmp_path)
    m = json.loads((d / "manifest.json").read_text())
    m["models"]["synreg"]["card"]["version"] = "v9"
    m["models"]["synreg"]["model_version_id"] = str(model_version_id("synreg", "v9", m["models"]["synreg"]["weights_sha256"]))
    (d / "manifest.json").write_text(json.dumps(m))
    assert sync_db.sync(pg, d, pending)["synreg"] == "draft"
    with psycopg.connect(pg) as c:
        assert c.execute("SELECT status, licenses_cleared FROM model_version WHERE version='v9'").fetchone() == ("draft", False)
    m["models"]["synreg"]["weights_sha256"] = "a" * 64
    (d / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(sync_db.SyncConflict):
        sync_db.sync(pg, d, pending)


def test_f79_cli_requires_migration_url(monkeypatch, capsys):
    from ml.registry import sync_db

    monkeypatch.delenv("METIS_MIGRATION_URL", raising=False)
    assert sync_db.main([]) == 2
