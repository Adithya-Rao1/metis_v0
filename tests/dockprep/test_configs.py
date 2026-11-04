# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310
import pytest

from backend.dockprep.configs import gnina_config, vina_config
from backend.dockprep.errors import DockprepError


def _kv(text):
    return {k.strip(): v.strip() for k, v in (ln.split("=", 1) for ln in text.splitlines() if "=" in ln and not ln.startswith("#"))}


def test_vina_config_content():
    t = vina_config([1.5, -2.25, 3.0], [20, 22, 24])
    kv = _kv(t)
    assert kv["receptor"] == "receptor.pdbqt" and kv["ligand"] == "ligand.pdbqt" and kv["out"] == "vina_out.pdbqt"
    assert (kv["center_x"], kv["center_y"], kv["center_z"]) == ("1.500", "-2.250", "3.000")
    assert (kv["size_x"], kv["size_y"], kv["size_z"]) == ("20.000", "22.000", "24.000")
    assert kv["exhaustiveness"] == "8" and kv["num_modes"] == "9" and "cnn_scoring" not in kv


def test_gnina_config_cnn_flag():
    off = gnina_config([0, 0, 0], [15, 15, 15])
    on = gnina_config([0, 0, 0], [15, 15, 15], cnn_scoring=True, exhaustiveness=16)
    assert _kv(off)["cnn_scoring"] == "none" and "OFF" in off
    assert _kv(on)["cnn_scoring"] == "rescore" and "ON" in on and _kv(on)["exhaustiveness"] == "16"
    assert _kv(on)["ligand"] == "ligand.sdf" and _kv(on)["out"] == "gnina_out.sdf"


def test_config_validation_and_name_sanitising():
    for bad in (dict(center=[0, 0], size=[10, 10, 10]), dict(center=[0, 0, float("nan")], size=[10, 10, 10]), dict(center=[0, 0, 0], size=[0, 10, 10]),
                dict(center=[0, 0, 0], size=[10, 10, 500])):
        with pytest.raises(DockprepError):
            vina_config(**bad)
    with pytest.raises(DockprepError):
        vina_config([0, 0, 0], [10, 10, 10], exhaustiveness=0)
    t = vina_config([0, 0, 0], [10, 10, 10], receptor="../../x y\nexhaustiveness = 999.pdbqt")
    assert "\nexhaustiveness = 999" not in t and "../" not in t
