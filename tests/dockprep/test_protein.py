# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310

import pytest
from _fixtures import DATA, build_1m17_like

from backend.dockprep.pdbio import parse_structure
from backend.dockprep.pdbqt import parse_pdbqt_atoms
from backend.dockprep.protein import find_pdb2pqr, prepare_protein


@pytest.fixture(scope="module")
def prep_noh(pdb_1m17):
    return prepare_protein(pdb_1m17.encode(), add_h=False, pdb_id="1M17")


def test_waters_removed_and_every_one_logged(prep_noh, pdb_1m17):
    in_waters = {ln[17:27] for ln in pdb_1m17.splitlines() if ln.startswith("HETATM") and ln[17:20] == "HOH"}
    assert len(in_waters) == 12
    logged = [e for e in prep_noh["change_log"] if e["action"] == "water removed"]
    assert len(logged) == 12 and {e["target"] for e in logged} == {f"A:{2000 + i}" for i in range(1, 13)}
    assert "HOH" not in prep_noh["files"]["receptor.pdb"]
    assert prep_noh["report"]["n_waters_removed"] == 12


def test_erlotinib_identified_as_cocrystal_and_kept_separately(prep_noh):
    refs = prep_noh["report"]["cocrystal_ligands"]
    assert [r["resname"] for r in refs] == ["AQ4"] and refs[0]["n_heavy_atoms"] == 29
    assert refs[0]["name_in_entry"] == "ERLOTINIB"
    ref = prep_noh["files"][refs[0]["file"]]
    assert "AQ4" in ref and "AQ4" not in prep_noh["files"]["receptor.pdb"]
    assert any(e["action"] == "co-crystal ligand identified" for e in prep_noh["change_log"])
    assert any(e["action"] == "removed from receptor" and "AQ4" in e["target"] for e in prep_noh["change_log"])


def test_keep_hets_and_waters_options(pdb_1m17):
    r = prepare_protein(pdb_1m17.encode(), add_h=False, remove_waters=False, keep_hets=["AQ4"])
    assert "AQ4" in r["files"]["receptor.pdb"] and "HOH" in r["files"]["receptor.pdb"]
    assert any(e["action"] == "kept in receptor" and "AQ4" in e["target"] for e in r["change_log"])
    assert not any(e["action"] == "water removed" for e in r["change_log"])


def test_altloc_keeps_highest_occupancy(prep_noh, pdb_1m17):
    e = [x for x in prep_noh["change_log"] if x["step"] == "altloc"]
    assert len(e) == 1 and e[0]["params"]["kept"] == "A" and e[0]["params"]["mean_occupancy_by_altloc"] == {"A": 0.6, "B": 0.4}
    st = parse_structure(prep_noh["files"]["receptor.pdb"].encode())
    assert all(a.altloc == "" for a in st.atoms)
    # flip occupancies: B must win
    flipped = pdb_1m17.replace("  0.60", "  0.30")
    r = prepare_protein(flipped.encode(), add_h=False)
    assert next(x for x in r["change_log"] if x["step"] == "altloc")["params"]["kept"] == "B"


def test_missing_residues_reported_and_pdbqt_parses(prep_noh):
    seg = prep_noh["report"]["missing_residue_segments"]
    assert seg == [{"chain": "A", "first": 669, "last": 671, "n": 3}]
    assert prep_noh["report"]["chain_breaks"]
    atoms = parse_pdbqt_atoms(prep_noh["files"]["receptor.pdbqt"])
    n_pdb = sum(1 for ln in prep_noh["files"]["receptor.pdb"].splitlines() if ln.startswith("ATOM"))
    assert len(atoms) == n_pdb and {a["type"] for a in atoms} >= {"C", "A", "N", "OA"}


def test_missing_atoms_detected():
    pdb = build_1m17_like(altloc=False)
    lines = [ln for ln in pdb.splitlines() if not (ln[12:16].strip() == "NZ" and ln[17:20] == "LYS" and ln[22:26].strip() == "684")]
    r = prepare_protein("\n".join(lines).encode(), add_h=False)
    assert any("NZ" in v for v in r["report"]["residues_missing_atoms"].values())


def test_chain_selection_error():
    from backend.dockprep.errors import DockprepError

    with pytest.raises(DockprepError):
        prepare_protein(build_1m17_like().encode(), chains=["Z"])


def test_cryoem_synthetic_header_parsed():
    pdb = build_1m17_like(method="ELECTRON MICROSCOPY", resolution="3.40")
    st = parse_structure(pdb.encode())
    assert (st.method_class, st.resolution, st.resolution_source) == ("cryoem", 3.4, "REMARK 2")
    r = prepare_protein(pdb.encode(), add_h=False)
    assert r["report"]["method_class"] == "cryoem" and "Cryo-EM" in r["report"]["method_note"]


def test_xray_header_and_refmac_resolution():
    st = parse_structure(build_1m17_like().encode())
    assert (st.method_class, st.resolution, st.pdb_id) == ("xray", 2.6, "1M17")
    pdb = "EXPDTA    X-RAY DIFFRACTION\nREMARK   3   RESOLUTION RANGE HIGH (ANGSTROMS) :   1.95\n" + "\n".join(
        ln for ln in build_1m17_like(with_header=False).splitlines())
    assert parse_structure(pdb.encode()).resolution == 1.95


def test_mmcif_minimal():
    cif = """data_TEST
_exptl.method 'ELECTRON MICROSCOPY'
_em_3d_reconstruction.resolution 3.1
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_alt_id
_atom_site.label_comp_id
_atom_site.auth_asym_id
_atom_site.auth_seq_id
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
_atom_site.occupancy
_atom_site.B_iso_or_equiv
_atom_site.pdbx_PDB_model_num
ATOM 1 N N . GLY A 1 1.0 2.0 3.0 1.00 20.0 1
ATOM 2 C CA . GLY A 1 2.0 2.0 3.0 1.00 20.0 1
ATOM 3 C C . GLY A 1 3.0 2.5 3.0 1.00 20.0 1
ATOM 4 N N . GLY A 1 9.0 9.0 9.0 1.00 20.0 2
"""
    st = parse_structure(cif.encode())
    assert len(st.atoms) == 3 and st.method_class == "cryoem" and st.resolution == 3.1 and st.source_format == "mmcif"


def test_other_structure_variety_no_ligand():
    data = (DATA / "hiv1_protease.pdb").read_bytes()
    r = prepare_protein(data, add_h=False)
    assert r["report"]["cocrystal_ligands"] == [] and set(r["report"]["chains_available"]) >= {"A"}


@pytest.mark.skipif(find_pdb2pqr() is None, reason="pdb2pqr not installed")
def test_hydrogens_with_pdb2pqr(pdb_1m17):
    r = prepare_protein(pdb_1m17.encode(), add_h=True, ph=7.4)
    assert r["report"]["hydrogens"]["pdb2pqr"] and r["report"]["hydrogens"]["hydrogens_added"] > 1000
    h = next(e for e in r["change_log"] if e["action"] == "hydrogens added")
    assert "PROPKA" in h["params"]["method"] and h["params"]["ph"] == 7.4
    atoms = parse_pdbqt_atoms(r["files"]["receptor.pdbqt"])
    assert any(a["type"] == "HD" for a in atoms)
    assert any(e["step"] == "protonation" for e in r["change_log"])


def test_fallback_without_pdb2pqr_logs_method(monkeypatch, pdb_1m17):
    monkeypatch.setattr("backend.dockprep.protein.find_pdb2pqr", lambda: None)
    r = prepare_protein(pdb_1m17.encode(), add_h=True, ph=7.4)
    e = next(x for x in r["change_log"] if x["step"] == "hydrogens")
    assert "not available" in e["action"] and r["report"]["hydrogens"]["hydrogens_added"] == 0
