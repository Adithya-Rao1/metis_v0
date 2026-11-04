# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310

import numpy as np
import pytest
from _fixtures import DATA, build_1m17_like, erlotinib_hetatm_lines

from backend.dockprep import pockets
from backend.dockprep.errors import DockprepError
from backend.dockprep.pdbio import parse_structure
from backend.dockprep.sites import analyze_sites


def _lig_center():
    _, mol = erlotinib_hetatm_lines()
    c = mol.GetConformer()
    xs = np.array([[c.GetAtomPosition(i).x, c.GetAtomPosition(i).y, c.GetAtomPosition(i).z] for i in range(mol.GetNumAtoms())])
    return xs, (xs.min(0) + xs.max(0)) / 2


def test_box_centered_on_cocrystal_ligand(pdb_1m17):
    r = analyze_sites(pdb_1m17.encode(), ["ligand"], padding=4.0)
    s = r["sites"][0]
    xs, bc = _lig_center()
    assert np.allclose(s["center"], bc, atol=0.01)
    ext = xs.max(0) - xs.min(0)
    assert np.allclose(s["size"], np.maximum(ext + 8.0, 10.0), atol=0.01)
    assert s["residues"] and any("LYS" in x for x in s["residues"])


def test_user_residues_box_and_errors(pdb_1m17):
    r = analyze_sites(pdb_1m17.encode(), ["residues"], residues=["A:694", "A:721"], padding=5.0)
    s = r["sites"][0]
    assert s["padding_A"] == 5.0 and s["residues"] == ["A:694", "A:721"]
    with pytest.raises(DockprepError):
        analyze_sites(pdb_1m17.encode(), ["residues"], residues=["A:99999"])
    with pytest.raises(DockprepError):
        analyze_sites(pdb_1m17.encode(), ["residues"], residues=["bad id"])
    with pytest.raises(DockprepError):
        analyze_sites(pdb_1m17.encode(), ["box"], box={"center": [0, 0, 0], "size": [0, 5, 5]})


def test_manual_box(pdb_1m17):
    s = analyze_sites(pdb_1m17.encode(), ["box"], box={"center": [1, 2, 3], "size": [20, 20, 20]})["sites"][0]
    assert s["center"] == [1.0, 2.0, 3.0] and s["size"] == [20.0, 20.0, 20.0]


def test_detected_pocket_agrees_with_ligand_site(pdb_1m17):
    r = analyze_sites(pdb_1m17.encode(), ["ligand", "detect", "literature"])
    lig = next(s for s in r["sites"] if s["source"] == "cocrystal_ligand")
    det = [s for s in r["sites"] if s["source"] == "detected"]
    assert det and all(s["score"]["name"] and s["score"]["method_note"] for s in det)
    top = max(det, key=lambda s: s["score"]["value"])
    shared = set(top["residues"]) & set(lig["residues"])
    assert len(shared) / len(set(lig["residues"])) >= 0.5      # top detected pocket is lined by most of the erlotinib-contact residues
    assert np.linalg.norm(np.array(top["center"]) - np.array(lig["center"])) < 10.0
    assert pockets.GEOMETRIC_METHOD in lig["agreement"]["methods_agreeing"]
    assert top["agreement"]["n_agreeing"] >= 1 and "rule" in top["agreement"]
    assert any("geometric fallback" in n or "fpocket" in n for n in r["notes"]) or pockets.find_fpocket()


def test_literature_sites_only_from_entry_annotations(pdb_1m17):
    r = analyze_sites(pdb_1m17.encode(), ["literature"])
    lit = r["sites"]
    assert lit and all(s["source"] == "literature" for s in lit)
    site = next(s for s in lit if s["citation"]["records"] == "SITE / REMARK 800")
    assert site["citation"]["source"] == "PDB entry 1M17" and site["citation"]["evidence_code"] == "SOFTWARE"
    assert "not an independent literature review" in site["citation"]["note"]
    assert "A:694 LEU" in site["residues"]
    # no annotations -> nothing asserted
    bare = analyze_sites((DATA / "egfr.pdb").read_bytes(), ["literature"])
    assert bare["sites"] == [] and "No SITE" in bare["notes"][0]


def test_uncertainty_low_resolution_bfactor_missing_loop_flex():
    pdb = build_1m17_like(resolution="3.80")
    # raise B-factors of pocket residue LEU 694 and add REMARK 465 residues adjacent to a pocket residue
    lines = []
    for ln in pdb.splitlines():
        if ln.startswith("ATOM") and ln[22:26].strip() == "694":
            ln = ln[:60] + "250.00" + ln[66:]
        lines.append(ln)
    pdb = "\n".join(lines).replace("REMARK 465     GLY A   669", "REMARK 465     GLY A   695\nREMARK 465     GLY A   669")
    r = analyze_sites(pdb.encode(), ["ligand"])
    f = {x["kind"]: x for x in r["sites"][0]["uncertainty"]}
    assert f["method_and_resolution"]["applies"] and f["method_and_resolution"]["observed"]["resolution_A"] == 3.8
    assert "threshold_used" in f["method_and_resolution"]
    assert "A:694" in f["high_b_factors"]["residues"] and f["high_b_factors"]["applies"]
    assert f["missing_residues_near_site"]["applies"] and "A:695" in f["missing_residues_near_site"]["residues"]
    assert f["flexible_residues"]["observed"]["long_flexible_side_chains_listed_for_reference"] is not None


def test_uncertainty_altloc_flexible_residue_listed():
    r = analyze_sites(build_1m17_like().encode(), ["residues"], residues=["A:684"])
    f = {x["kind"]: x for x in r["sites"][0]["uncertainty"]}
    assert f["flexible_residues"]["applies"] and f["flexible_residues"]["observed"]["alternate_conformations_in_file"] == ["A:684"]


def test_no_verdict_language_in_uncertainty(pdb_1m17):
    import json

    text = json.dumps(analyze_sites(pdb_1m17.encode(), ["ligand", "literature"])).lower()
    for w in ("good", "bad", "reliable", "unreliable", "recommended", "poor", "excellent"):
        assert f" {w} " not in text and f'"{w}' not in text


def test_fpocket_output_parser_on_fixture():
    d = DATA / "fpocket_fixture"
    info = pockets.parse_fpocket_info((d / "prot_info.txt").read_text())
    assert [p["rank"] for p in info] == [1, 2] and info[0]["score"] == 0.612 and info[0]["number_of_alpha_spheres"] == 52
    pts = pockets.parse_fpocket_vertices((d / "pocket1_vert.pqr").read_text())
    assert pts.shape == (3, 3) and np.allclose(pts.mean(0), [22.1, 0.0, 53.0])


@pytest.mark.skipif(pockets.find_fpocket() is None, reason="fpocket binary not installed")
def test_fpocket_run(pdb_1m17):
    st = parse_structure(pdb_1m17.encode())
    res = pockets.run_fpocket(st.atoms)
    assert res
