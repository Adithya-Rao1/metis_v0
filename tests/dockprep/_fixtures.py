# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310
"""Builds test structures. The protein atoms are the cleaned 1M17 chain A from egfr.pdb and the ligand is
erlotinib from egfr_reference.sdf (same coordinate frame). Header records, waters, an alternate-location residue,
SITE/REMARK 800 and REMARK 465 are SYNTHETIC additions (the cleaned source file has none), so tests can exercise
parsing paths. No network is used."""
from __future__ import annotations

import math
from pathlib import Path

from rdkit import Chem

DATA = Path(__file__).parent / "data"


def erlotinib_hetatm_lines(resname="AQ4", chain="A", resseq=999):
    mol = Chem.MolFromMolFile(str(DATA / "egfr_reference.sdf"), removeHs=True)
    conf = mol.GetConformer()
    out, counts = [], {}
    for a in mol.GetAtoms():
        s = a.GetSymbol()
        counts[s] = counts.get(s, 0) + 1
        p = conf.GetAtomPosition(a.GetIdx())
        nm = f"{s}{counts[s]}"
        out.append(f"HETATM{5000 + a.GetIdx():5d} {nm:<4} {resname:>3} {chain}{resseq:4d}    {p.x:8.3f}{p.y:8.3f}{p.z:8.3f}  1.00 20.00          {s:>2}")
    return out, mol


def build_1m17_like(n_waters=12, with_header=True, method="X-RAY DIFFRACTION", resolution="2.60", altloc=True) -> str:
    base = [ln for ln in (DATA / "egfr.pdb").read_text().splitlines() if ln.startswith("ATOM")]
    het, mol = erlotinib_hetatm_lines()
    conf = mol.GetConformer()
    pts = [conf.GetAtomPosition(i) for i in range(mol.GetNumAtoms())]
    cx = sum(p.x for p in pts) / len(pts); cy = sum(p.y for p in pts) / len(pts); cz = sum(p.z for p in pts) / len(pts)
    prot_xyz = [(float(l[30:38]), float(l[38:46]), float(l[46:54])) for l in base]
    waters = []
    k = 0
    for i in range(400):
        ang = i * 2.399963
        r = 6 + (i % 9)
        z = (i % 7 - 3) * 2.0
        w = (cx + r * math.cos(ang), cy + r * math.sin(ang), cz + z)
        if min(math.dist(w, q) for q in prot_xyz) > 2.8 and min(math.dist(w, (p.x, p.y, p.z)) for p in pts) > 2.8:
            k += 1
            waters.append(f"HETATM{6000 + k:5d}  O   HOH A{2000 + k:4d}    {w[0]:8.3f}{w[1]:8.3f}{w[2]:8.3f}  1.00 30.00           O")
        if k >= n_waters:
            break
    lines = []
    if with_header:
        lines += [
            "HEADER    TRANSFERASE/TRANSFERASE INHIBITOR       01-APR-02   1M17              ",
            f"EXPDTA    {method}",
            "REMARK   2",
            f"REMARK   2 RESOLUTION.    {resolution} ANGSTROMS.",
            "REMARK 465 MISSING RESIDUES",
            "REMARK 465   M RES C SSSEQI",
            "REMARK 465     GLY A   669",
            "REMARK 465     SER A   670",
            "REMARK 465     LEU A   671",
            "REMARK 800",
            "REMARK 800 SITE",
            "REMARK 800 SITE_IDENTIFIER: AC1",
            "REMARK 800 EVIDENCE_CODE: SOFTWARE",
            "REMARK 800 SITE_DESCRIPTION: BINDING SITE FOR RESIDUE AQ4 A 999",
            "HET    AQ4  A 999      29                                                       ",
            "HETNAM     AQ4 ERLOTINIB                                                        ",
            "SITE     1 AC1  6 LEU A 694  VAL A 702  ALA A 719  LYS A 721  MET A 769  AQ4 A 999",
        ]
    prot = list(base)
    if altloc:
        # duplicate side-chain atoms of the first LYS to make altlocs A (0.60) and B (0.40)
        out = []
        done = False
        for ln in prot:
            if not done and ln[17:20] == "LYS" and ln[12:16].strip() in ("CD", "CE", "NZ"):
                a = ln[:16] + "A" + ln[17:54] + "  0.60" + ln[60:]
                b = ln[:16] + "B" + ln[17:30] + f"{float(ln[30:38]) + 0.8:8.3f}" + ln[38:54] + "  0.40" + ln[60:]
                out += [a, b]
                if ln[12:16].strip() == "NZ":
                    done = True
            else:
                out.append(ln)
        prot = out
    return "\n".join(lines + prot + ["TER"] + het + waters + ["END"]) + "\n"
