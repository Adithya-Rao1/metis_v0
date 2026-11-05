import json
from pathlib import Path

import numpy as np
import pytest
import yaml

from ml.data.manifest import LicenseError
from ml.train import train_endpoint as TE
from ml.train.ensemble import POOL_SIZE, EnsemblePool, member_seed, member_subset, scaffold_groups
from ml.train.member import SklearnMember, get_member_cls

FAST = {"n_estimators": 5}


def cfg(ds, kind, **kw):
    return {"endpoint": "synthetic", "version": "v1", "datasets": [ds], "kind": kind, "backend": "sklearn",
            "member_params": FAST, "seed": 3, **kw}


def loader_for(mols, y, ds):
    return lambda c: [(s, float(v), ds) for s, v in zip(mols, y)]


def test_pool_has_20_distinct_seeds_and_resampled_subsets(mols):
    assert POOL_SIZE == 20
    seeds = [member_seed(0, k) for k in range(20)]
    assert len(set(seeds)) == 20
    g = scaffold_groups(mols)
    subs = [tuple(member_subset(g, s)) for s in seeds]
    assert len(set(subs)) >= 10  # synthetic data has only 8 scaffolds, so some subsets coincide; real data has many
    assert all(0.5 < len(s) / len(mols) < 1.0 for s in subs)
    # scaffold groups stay whole inside a subset
    sub = set(member_subset(g, seeds[0]))
    assert all(set(grp) <= sub or not (set(grp) & sub) for grp in g)


def test_pool_deterministic_and_resumable(mols, yreg, tmp_path):
    a = EnsemblePool(SklearnMember, "regression", FAST, 20, 7).train(mols[:80], yreg[:80], tmp_path / "ck")
    events = []
    b = EnsemblePool(SklearnMember, "regression", FAST, 20, 7).train(mols[:80], yreg[:80], tmp_path / "ck",
                                                                    on_member=lambda k, r: events.append(r))
    assert all(events) and len(events) == 20  # all reused from checkpoints
    assert np.allclose(a.predict_members(mols[80:90]), b.predict_members(mols[80:90]))
    c = EnsemblePool(SklearnMember, "regression", FAST, 20, 7).train(mols[:80], yreg[:80])  # fresh, no ckpt
    assert np.allclose(a.predict_members(mols[80:90]), c.predict_members(mols[80:90]))
    # partial resume: delete some checkpoints, only those retrain
    for k in (3, 11):
        (tmp_path / "ck" / f"member_{k:02d}.ckpt").unlink()
    ev = []
    EnsemblePool(SklearnMember, "regression", FAST, 20, 7).train(mols[:80], yreg[:80], tmp_path / "ck", on_member=lambda k, r: ev.append((k, r)))
    assert [k for k, r in ev if not r] == [3, 11]
    # different seed changes a stale checkpoint -> retrained, not silently reused
    ev = []
    EnsemblePool(SklearnMember, "regression", FAST, 20, 8).train(mols[:80], yreg[:80], tmp_path / "ck", on_member=lambda k, r: ev.append(r))
    assert not any(ev)


def test_first_N_members_are_prefixes(mols, yreg):
    p = EnsemblePool(SklearnMember, "regression", FAST, 20, 0).train(mols[:60], yreg[:60])
    assert np.allclose(p.predict_members(mols[:5], n=5), p.predict_members(mols[:5])[:5])


def test_cli_refuses_today_on_every_shipped_config(tmp_path, capsys):
    cfgs = sorted(Path("ml/train/configs").glob("*.yaml"))
    assert len(cfgs) >= 10
    for c in cfgs:
        assert TE.main(["--config", str(c), "--artifacts-dir", str(tmp_path)]) == 2
        assert "REFUSED" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []  # nothing written


def test_run_training_refuses_pending_and_excluded_before_reading_data(tmp_path):
    called = []
    def loader(c):
        called.append(1)
        return []
    for ds in ("hERG", "FreeSolv"):
        with pytest.raises(LicenseError):
            TE.run_training(cfg(ds, "classification" if ds == "hERG" else "regression"), loader=loader, artifacts_root=tmp_path)
    assert called == []


def test_end_to_end_with_cleared_synthetic(cleared_manifest, mols, yreg, tmp_path):
    c = cfg("SynthReg", "regression", n_members=20)
    root = TE.run_training(c, manifest_path=cleared_manifest, loader=loader_for(mols, yreg, "SynthReg"), artifacts_root=tmp_path)
    meta = json.loads((root / "meta.json").read_text())
    assert meta["member_seeds"] == [3000 + k for k in range(20)] and meta["n_members"] == 20
    assert meta["split"] == "scaffold" and meta["default_range"][0] < meta["default_range"][1]
    assert meta["datasets"][0]["sha256"] and len(list((root / "members").glob("*.ckpt"))) == 20
    cal = json.loads((root / "calibration.json").read_text())
    assert set(cal["conformal"]) == {"3", "5", "10", "20"}
    # kind mismatch is refused
    with pytest.raises(ValueError):
        TE.run_training(cfg("SynthReg", "classification"), manifest_path=cleared_manifest, loader=lambda c: [], artifacts_root=tmp_path)


def test_config_validation(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"endpoint": "x", "version": "v", "datasets": ["hERG"], "kind": "classification", "backend": "sklearn", "split": "random"}))
    with pytest.raises(ValueError):
        TE.load_config(p)
    p.write_text(yaml.safe_dump({"endpoint": "x"}))
    with pytest.raises(ValueError):
        TE.load_config(p)


def test_chemprop_adapter_imports_and_trains_tiny(tmp_path):
    pytest.importorskip("chemprop")
    from ml.train.chemprop_member import ChempropMember
    assert get_member_cls("chemprop") is ChempropMember
    from ml_synth import make_molecules, regression_labels
    m = make_molecules(20, 5)
    y = regression_labels(m)
    mem = ChempropMember("regression", max_epochs=1, hidden_dim=16, batch_size=8).fit(m, y, 0)
    p = mem.predict(m[:4])
    assert p.shape == (4,) and np.isfinite(p).all()
    mem.save(tmp_path / "m.ckpt")
    assert np.allclose(ChempropMember.load(tmp_path / "m.ckpt").predict(m[:4]), p, atol=1e-5)


def test_higher_is_better_config_key_is_bool_or_absent_and_shipped_configs_obey(tmp_path):
    import yaml

    base = {"endpoint": "e", "version": "v1", "kind": "regression", "datasets": ["D"], "backend": "sklearn"}
    for good in (None, True, False):
        p = tmp_path / "ok.yaml"
        p.write_text(yaml.safe_dump({**base, "higher_is_better": good}))
        assert TE.load_config(p)["higher_is_better"] is good
    for bad in ("yes", 1, 0, "true", []):
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.safe_dump({**base, "higher_is_better": bad}))
        with pytest.raises(ValueError, match="higher_is_better"):
            TE.load_config(p)
    for f in sorted((Path(TE.__file__).parent / "configs").glob("*.yaml")):
        v = TE.load_config(f).get("higher_is_better")
        assert v is None or isinstance(v, bool), f.name
