# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310
import io

from rdkit import Chem

from backend.dockprep.ligand import prepare_ligand
from backend.dockprep.pdbqt import parse_pdbqt_atoms, validate_ligand_pdbqt

ERLO = "COCCOc1cc2ncnc(Nc3cccc(c3)C#C)c2cc1OCCOC"


def _sdf_mol(text):
    return next(iter(Chem.ForwardSDMolSupplier(io.BytesIO(text.encode()), removeHs=False)))


def test_erlotinib_prep_files_parse():
    r = prepare_ligand(smiles=ERLO + ".Cl", name="erlotinib", ph=7.4, n_conformers=5)
    pq = r["files"]["erlotinib.pdbqt"]
    info = validate_ligand_pdbqt(pq)
    atoms = parse_pdbqt_atoms(pq)
    assert info["atoms"] == len(atoms) and info["torsdof"] >= 8
    m = _sdf_mol(r["files"]["erlotinib.sdf"])
    assert m.GetNumConformers() == 1 and m.GetProp("MetisConformerMethod").startswith("ETKDGv3")
    assert m.GetProp("MetisNote") == "computed conformer, not an experimental structure"
    assert {a["type"] for a in atoms} <= {"C", "A", "N", "NA", "OA", "HD"}
    assert r["report"]["net_charge"] == 0


def test_salt_removal_and_log_completeness():
    r = prepare_ligand(smiles=ERLO + ".Cl")
    steps = [(e["step"], e["action"]) for e in r["change_log"]]
    assert ("standardize", "removed counter-ions/salt fragments") in steps
    for needed in ("parse", "standardize", "stereo", "protonate", "conformers", "pdbqt"):
        assert any(s == needed for s, _ in steps), needed
    prot = next(e for e in r["change_log"] if e["step"] == "protonate")
    assert prot["params"]["ph"] == 7.4 and "method" in prot["params"]


def test_ph_changes_ionisation():
    acid = "CC(=O)Oc1ccccc1C(=O)O"
    lo = prepare_ligand(smiles=acid, ph=2.0, n_conformers=2)["report"]["net_charge"]
    hi = prepare_ligand(smiles=acid, ph=7.4, n_conformers=2)["report"]["net_charge"]
    assert (lo, hi) == (0, -1)
    amine = "CCN(CC)CCNC(=O)c1ccc(N)cc1"
    assert prepare_ligand(smiles=amine, ph=7.4, n_conformers=2)["report"]["net_charge"] == 1
    assert prepare_ligand(smiles=amine, ph=12.0, n_conformers=2)["report"]["net_charge"] == 0
    # amide and aniline nitrogens must not be protonated
    assert prepare_ligand(smiles="CC(=O)Nc1ccccc1", ph=2.0, n_conformers=2)["report"]["net_charge"] == 0


def test_stereo_reported_and_sdf_input():
    r = prepare_ligand(smiles="CC(N)C(=O)O", n_conformers=2)
    st = next(e for e in r["change_log"] if e["step"] == "stereo")
    assert st["params"]["unspecified_atom_indices"]
    r2 = prepare_ligand(smiles="C[C@H](N)C(=O)O", n_conformers=2)
    assert not next(e for e in r2["change_log"] if e["step"] == "stereo")["params"]["unspecified_atom_indices"]
    sdf = (__import__("pathlib").Path(__file__).parent / "data" / "egfr_reference.sdf").read_text()
    r3 = prepare_ligand(sdf=sdf, n_conformers=3)
    assert r3["report"]["heavy_atoms"] == 29


def test_name_sanitized():
    r = prepare_ligand(smiles="CCO", name="../../etc/passwd", n_conformers=1)
    assert all("/" not in k and ".." not in k for k in r["files"])


def test_bad_ligand_rejected():
    import pytest

    from backend.dockprep.errors import DockprepError

    for kw in ({"smiles": "not a smiles ((("}, {"smiles": "C" * 3000}, {}, {"smiles": "CCO", "ph": 20}):
        with pytest.raises(DockprepError):
            prepare_ligand(**kw)
