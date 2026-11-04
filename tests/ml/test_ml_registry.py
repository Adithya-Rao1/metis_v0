import json
import shutil
from pathlib import Path

import pytest

from backend.schemas.interfaces import ModelCard, ModelRegistry, PredictionDetail
from ml.benchmarks.protocol import run_benchmark
from ml.data.manifest import LicenseError
from ml.registry.publish import PublishRefused, assert_real, publish
from ml.registry.registry import FileModelRegistry, NotTrained, UntrainedRegistry, default_registry
from ml.train import train_endpoint as TE
from ml.train.member import SklearnMember

FAST = {"n_estimators": 6}


def _train(root, manifest, mols, y, kind, ds, endpoint):
    cfg = {"endpoint": endpoint, "version": "v1", "datasets": [ds], "kind": kind, "backend": "sklearn",
           "member_params": FAST, "seed": 1, "n_members": 20}
    b = TE.run_training(cfg, manifest_path=manifest, loader=lambda c: [(s, float(v), ds) for s, v in zip(mols, y)],
                        artifacts_root=root / "artifacts")
    rep = run_benchmark(mols, y, kind, split_method="scaffold", member_cls=SklearnMember, member_params=FAST, endpoint=endpoint,
                        seed=1, frac=(0.7, 0.15, 0.15), rf_trees=10, run_cv=False)
    return b, rep


@pytest.fixture(scope="module")
def pub(tmp_path_factory, cleared_manifest, mols, yreg, ycls):
    root = tmp_path_factory.mktemp("reg")
    out = {"root": root}
    for kind, ds, ep, y in (("regression", "SynthReg", "synreg", yreg), ("classification", "SynthCls", "syncls", ycls)):
        b, rep = _train(root, cleared_manifest, mols, y, kind, ds, ep)
        card = publish(b, rep, registry_root=root, manifest_path=cleared_manifest)
        out[ep] = (b, rep, card)
    return out


def test_untrained_stub_raises_clear_errors():
    r = UntrainedRegistry()
    assert isinstance(r, ModelRegistry) and r.list_models() == []
    for call in (lambda: r.model_card("x"), lambda: r.predict("x", "CCO", 5), lambda: r.time_estimate_seconds("x", 5, 3)):
        with pytest.raises(NotTrained, match="not trained"):
            call()


def test_shipped_registry_is_untrained_today():
    r = default_registry()
    assert isinstance(r, UntrainedRegistry)
    f = FileModelRegistry()
    assert f.list_models() == []
    with pytest.raises(NotTrained):
        f.predict("solubility", "CCO", 5)


def test_publish_refuses_when_license_pending(pub, tmp_path):
    b, rep, _ = pub["synreg"]
    before = (pub["root"] / "manifest.json").read_text()
    with pytest.raises((LicenseError, KeyError)):  # shipped manifest: dataset unknown/pending -> refused
        publish(b, rep, registry_root=pub["root"])
    assert (pub["root"] / "manifest.json").read_text() == before


def test_publish_refuses_pending_dataset_in_manifest(pub, tmp_path, cleared_manifest):
    pending = tmp_path / "d.yaml"
    pending.write_text(cleared_manifest.read_text().replace("license_status: cleared", "license_status: pending", 1))
    b, rep, _ = pub["synreg"]
    with pytest.raises(LicenseError):
        publish(b, rep, registry_root=pub["root"], manifest_path=pending)


def test_registry_implements_protocol_and_cards_are_real(pub):
    reg = FileModelRegistry(pub["root"])
    assert isinstance(reg, ModelRegistry)
    cards = reg.list_models()
    assert {c.endpoint for c in cards} == {"synreg", "syncls"}
    c = reg.model_card("synreg")
    assert isinstance(c, ModelCard) and c.kind == "regression" and c.default_range[0] < c.default_range[1]
    assert set(c.coverage_by_n) == set(c.runtime_by_n_seconds) == {"3", "5", "10", "20"}
    assert all(v > 0 for v in c.runtime_by_n_seconds.values()) and len(c.weights_sha256) == 64
    assert c.datasets[0]["license"] and c.split_type == "scaffold"
    assert c.default_range == pytest.approx((pub["synreg"][0] and json.loads((pub["synreg"][0] / "meta.json").read_text())["default_range"][0],
                                             json.loads((pub["synreg"][0] / "meta.json").read_text())["default_range"][1]))
    with pytest.raises(NotTrained):
        reg.model_card("nothing")


def test_manifest_records_provenance(pub):
    m = json.loads((pub["root"] / "manifest.json").read_text())["models"]["synreg"]
    assert len(m["provenance"]["code_commit"]) == 40 and m["provenance"]["datasets"][0]["sha256"]
    assert m["provenance"]["seeds"]["member_seeds"][:2] == [1000, 1001] and len(m["provenance"]["member_files_sha256"]) >= 20
    assert set(m["calibration_by_n"]) == {"3", "5", "10", "20"} and m["benchmark"]["metrics"]["ensemble"]["rmse"] > 0
    assert m["weights_sha256"] == m["card"]["weights_sha256"]


def test_time_estimate_formula(pub):
    reg = FileModelRegistry(pub["root"])
    t = [reg.time_estimate_seconds("synreg", n, 10) for n in (3, 5, 10, 20)]
    assert all(x > 0 for x in t) and t[0] < t[-1]
    assert reg.time_estimate_seconds("synreg", 5, 10) > reg.time_estimate_seconds("synreg", 5, 1)
    with pytest.raises(ValueError):
        reg.time_estimate_seconds("synreg", 4, 3)


def test_predict_regression_and_classification(pub):
    reg = FileModelRegistry(pub["root"])
    d = reg.predict("synreg", "CCOc1ccccc1C", 5)
    assert isinstance(d, PredictionDetail)
    p = d.prediction
    assert p.interval_low <= p.value <= p.interval_high and p.ensemble_n == 5 and p.sample_size > 0
    assert "split conformal" in p.method_note and p.breakdown.assay_noise == 0
    assert abs(sum(f.share for f in d.fragments) - 1) < 1e-6 and len(d.nearest_training) == 3 and d.runtime_seconds > 0
    ood = reg.predict("synreg", "O=S(=O)(O)CCCCCCCCP(=O)(O)O", 5).prediction
    assert (ood.interval_high - ood.interval_low) > (p.interval_high - p.interval_low)
    c = reg.predict("syncls", "CCOc1ccccc1C", 10).prediction
    assert c.kind == "classification" and 0 <= c.probability <= 1 and c.interval_low is None
    with pytest.raises(ValueError):
        reg.predict("synreg", "CCO", 7)


def test_tampered_weights_refuse_to_load(pub, tmp_path):
    root = tmp_path / "copy"
    shutil.copytree(pub["root"], root)
    m = next((root / "artifacts" / "synreg" / "v1" / "members").glob("member_00.ckpt"))
    m.write_bytes(m.read_bytes() + b"x")
    with pytest.raises(ValueError, match="hash mismatch"):
        FileModelRegistry(root).predict("synreg", "CCO", 3)


def test_publish_refuses_placeholder_fields(pub):
    card = pub["synreg"][2]
    for mut in ({"limitations": "TODO"}, {"runtime_by_n_seconds": {"3": 1.0}}, {"default_range": (1.0, 1.0)},
                {"coverage_by_n": {"3": 0.9, "5": 0.9, "10": 0.9, "20": 1.5}}, {"weights_sha256": "abc"},
                {"runtime_by_n_seconds": {"3": 0.0, "5": 1.0, "10": 1.0, "20": 1.0}}):
        bad = card.model_copy(update=mut)
        with pytest.raises((PublishRefused, ValueError)):
            assert_real(bad)
    assert_real(card)


def test_publish_refuses_mismatched_benchmark(pub, cleared_manifest):
    b, rep, _ = pub["synreg"]
    with pytest.raises(PublishRefused):
        publish(b, {**rep, "split": "butina"}, registry_root=pub["root"], manifest_path=cleared_manifest)
