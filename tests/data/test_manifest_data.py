import pytest
from ml.data.manifest import load_manifest, assert_trainable, assert_loadable, LicenseError

EXPECTED = {"hERG","hERG_Karim","AMES","LD50_Zhu","DILI","Solubility_AqSolDB","Caco2_Wang","PAMPA_NCATS",
            "Half_Life_Obach","Clearance_Microsome_AZ","Clearance_Hepatocyte_AZ","Lipophilicity_AstraZeneca",
            "FreeSolv","Biogen_KineticSolubility"}


def test_entries_and_fields():
    m = load_manifest()
    assert set(m) == EXPECTED
    for e in m.values():
        assert e.license_status in {"cleared", "pending", "excluded"}
        assert e.date_checked is None and e.confirmed_by is None
    assert m["FreeSolv"].license_status == "excluded"
    assert "logD" in m["Lipophilicity_AstraZeneca"].endpoint


def test_all_tdc_pending_and_untrainable():
    for id_, e in load_manifest().items():
        if id_ != "FreeSolv":
            assert e.license_status == "pending"
        with pytest.raises(LicenseError):
            assert_trainable(id_)


def test_freesolv_never_loadable():
    with pytest.raises(LicenseError):
        assert_loadable("FreeSolv")
    assert assert_loadable("AMES").id == "AMES"


def test_cleared_requires_confirmation_and_trainable(tmp_path):
    base = (tmp_path / "m.yaml")
    base.write_text("datasets:\n  - {id: X, name: X, source_url: u, endpoint: e, kind: regression, units: u, license_as_stated: CC BY 4.0, license_status: cleared, date_checked: null, confirmed_by: null, notes: n}\n")
    with pytest.raises(ValueError):
        load_manifest(base)
    base.write_text(base.read_text().replace("date_checked: null, confirmed_by: null", "date_checked: 2026-01-01, confirmed_by: owner"))
    assert assert_trainable("X", base).id == "X"


def test_freesolv_refused_even_if_marked_cleared(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("datasets:\n  - {id: FreeSolv, name: F, source_url: u, endpoint: e, kind: regression, units: u, license_as_stated: s, license_status: cleared, date_checked: 2026-01-01, confirmed_by: x, notes: n}\n")
    with pytest.raises(LicenseError):
        assert_trainable("FreeSolv", p)
    with pytest.raises(LicenseError):
        assert_loadable("FreeSolv", p)


def _entry(**kw):
    d = dict(id="X", name="X", source_url="https://example.org/x", endpoint="e", kind="regression", units="u",
             license_as_stated="CC BY 4.0", license_status="cleared", date_checked="2026-01-01", confirmed_by="owner", notes="n")
    d.update(kw)
    return "datasets:\n  - " + "{" + ", ".join(f"{k}: {v!r}" for k, v in d.items()) + "}\n"


def _load(tmp_path, **kw):
    p = tmp_path / "m.yaml"
    p.write_text(_entry(**kw))
    return load_manifest(p)


def test_valid_cleared_entry_loads(tmp_path):
    assert "X" in _load(tmp_path)


@pytest.mark.parametrize("kw", [
    {"id": "SAMPL_hydration", "source_url": "https://github.com/MobleyLab/FreeSolv"},
    {"id": "FreeSolv2"},
    {"license_as_stated": "CC BY-NC-SA 4.0"},
    {"license_as_stated": "CC BY-ND 4.0"},
    {"license_as_stated": "Non-commercial research only"},
    {"license_as_stated": "unchecked"},
])
def test_f25_f26_cleared_rejects_freesolv_alias_and_nc(tmp_path, kw):
    with pytest.raises(ValueError):
        _load(tmp_path, **kw)


@pytest.mark.parametrize("kw", [
    {"date_checked": "not a date"}, {"date_checked": "2999-01-01"}, {"date_checked": ""},
    {"confirmed_by": "False"}, {"confirmed_by": ""}, {"confirmed_by": "x"},
])
def test_f26_cleared_requires_valid_provenance(tmp_path, kw):
    with pytest.raises(ValueError):
        _load(tmp_path, **kw)


@pytest.mark.parametrize("bad", ["../../evil", "a b", "a/b", "x.csv", "", "a-b"])
def test_f27_bad_ids_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        _load(tmp_path, id=bad, license_status="pending", date_checked=None, confirmed_by=None)
