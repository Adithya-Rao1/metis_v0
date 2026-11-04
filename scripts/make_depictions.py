#!/usr/bin/env python3
"""Generate the static assets of the Metis front end (web/) with RDKit.

Outputs (all deterministic):
  web/assets/benzene.svg            home page logo (RDKit depiction of benzene)
  web/assets/erlotinib.svg          2D fallback for the home page and docking prep
  web/fixtures/svg/*.svg            2D depictions (plain + fragment-highlighted) for FIXTURE compounds
  web/fixtures/project.json         FIXTURE project: compounds, predictions, conformers (molblocks)
  web/fixtures/training.json        FIXTURE stand-in training compounds (nearest-neighbour lists)
  web/fixtures/structures/*         trimmed 1M17 chain A + erlotinib (pocket shell kept in full)
  web/legal/*.html, web/js/legal_text.js   rendered copies of docs/legal drafts (read-only inputs)

All numbers in fixtures are illustrative layout content, NOT model results.
Run:  python3 scripts/make_depictions.py [--egfr-pdb PATH] [--egfr-sdf PATH]
"""
from __future__ import annotations

import argparse
import html
import json
import math
import random
import re
from pathlib import Path

from rdkit import Chem, DataStructs, RDLogger
from rdkit.Chem import AllChem, rdFingerprintGenerator
from rdkit.Chem.Draw import rdMolDraw2D

RDLogger.DisableLog("rdApp.*")
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DEF_PDB = "/Users/adithyarao/.superset/worktrees/GraphDQN/demo/main/studio/data/structures/egfr.pdb"
DEF_SDF = "/Users/adithyarao/.superset/worktrees/GraphDQN/demo/main/studio/data/ligands/egfr_reference.sdf"
CONFORMER_LABEL = "computed conformer, not an experimental structure"
CONFORMER_METHOD = "RDKit ETKDGv3 embedding (seed 7) + MMFF94 optimisation, explicit hydrogens"
FIXTURE_UUID = "00000000-0000-4000-8000-000000000001"

# ---- fixture compounds (public drug-like structures used only as layout content) ----
# id, smiles, fragment name, fragment SMARTS
COMPOUNDS = [
    (1, "COCCOc1cc2ncnc(Nc3cccc(C#C)c3)c2cc1OCCOC", "quinazoline core", "c1ncnc2ccccc12"),
    (2, "CC(C)NCC(O)COc1cccc2ccccc12", "naphthalene", "c1ccc2ccccc2c1"),
    (3, "Cc1ccc(cc1)S(=O)(=O)Nc1ccc(cc1)N1CCN(C)CC1", "sulfonamide", "S(=O)(=O)N"),
    (4, "OC1(CCN(CCCC(=O)c2ccc(F)cc2)CC1)c1ccc(Cl)cc1", "4-fluorophenyl ketone", "O=C(CC)c1ccc(F)cc1"),
    (5, "CN1C(=O)CN=C(c2ccccc2)c2cc(Cl)ccc21", "aryl ketimine", "c-C=N"),
    (6, "Cc1ccc(cc1)-c1cc(nn1-c1ccc(cc1)S(N)(=O)=O)C(F)(F)F", "sulfonamide", "S(N)(=O)=O"),
    (7, "CC(C)Cc1ccc(cc1)C(C)C(=O)O", "carboxylic acid", "C(=O)[OH]"),
    (8, "Cn1cnc2c1c(=O)n(C)c(=O)n2C", "N,N-dimethyl uracil-like ring", "c(=O)n(C)c(=O)"),
]
# stand-in "training set" for nearest-neighbour fixtures (NOT the real training data)
TRAIN_SMILES = [
    "CC(=O)Oc1ccccc1C(=O)O", "CC(=O)Nc1ccc(O)cc1", "COc1ccc2cc(C(C)C(=O)O)ccc2c1",
    "CCN(CC)CC(=O)Nc1c(C)cccc1C", "COCCc1ccc(OCC(O)CNC(C)C)cc1", "NC(=O)c1ccc(OCC(O)CNC(C)C)cc1",
    "CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O", "Cc1cc(NS(=O)(=O)c2ccc(N)cc2)no1",
    "CCCCNC(=O)NS(=O)(=O)c1ccc(C)cc1", "CN(C)CCCN1c2ccccc2Sc2ccc(Cl)cc21", "CN(C)CCCN1c2ccccc2CCc2ccccc21",
    "O=C1NC(=O)C(c2ccccc2)(c2ccccc2)N1", "Cn1c(=O)c2[nH]cnc2n(C)c1=O",
    "COC(=O)C1=C(C)NC(C)=C(C(=O)OC)C1c1ccccc1[N+](=O)[O-]", "COCCOc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OC",
    "CC(C)NCC(O)c1ccc(O)c(C(N)=O)c1", "CC(C)NCC(O)COc1cccc2[nH]ccc12", "CCCCOC(=O)c1ccc(N)cc1",
    "CCCc1nc(C)c2c(=O)[nH]c(-c3cc(S(=O)(=O)N4CCN(C)CC4)ccc3OCC)nn12", "Cc1ccccc1NC(=O)CN(C)C",
]

# endpoint -> info. 'range' is the fixture reference range used ONLY for the desirability scaling in ranking.
ENDPOINTS = {
    "solubility": dict(label="Solubility", unit="logS, thermodynamic", kind="regression", higher=True,
                       range=[-7.0, 0.0], cal_width=1.6, cal_n=1000, dataset="AqSolDB (thermodynamic)",
                       tdc="Solubility_AqSolDB", pos=None),
    "permeability": dict(label="Permeability", unit="Caco-2, log cm/s", kind="regression", higher=True,
                         range=[-7.0, -4.0], cal_width=1.4, cal_n=150, dataset="Caco2_Wang",
                         tdc="Caco2_Wang", pos=None),
    "herg": dict(label="hERG", unit="P(blocker)", kind="classification", higher=False, range=[0, 1],
                 cal_width=None, cal_n=1240, dataset="hERG_Karim", tdc="hERG_Karim", pos=["blocker", "non-blocker"]),
    "ames": dict(label="AMES", unit="P(mutagenic)", kind="classification", higher=False, range=[0, 1],
                 cal_width=None, cal_n=1100, dataset="AMES", tdc="AMES", pos=["mutagenic", "non-mutagenic"]),
    "ld50": dict(label="LD50", unit="rodent, log mmol/kg", kind="regression", higher=True, range=[0.5, 4.0],
                 cal_width=1.5, cal_n=1500, dataset="LD50_Zhu", tdc="LD50_Zhu", pos=None),
}
# per compound: solubility, permeability (value, lo, hi); herg (p, n_near); ames (p, n_near); ld50
VALUES = {
    1: dict(solubility=(-3.1, -3.9, -2.4), permeability=(-4.9, -5.6, -4.3), herg=(0.18, 214), ames=(0.07, 388), ld50=(2.4, 1.9, 3.0)),
    2: dict(solubility=(-4.4, -5.9, -3.0), permeability=(-5.2, -6.0, -4.5), herg=(0.21, 96), ames=(0.12, 402), ld50=(2.1, 1.4, 2.9)),
    3: dict(solubility=(-2.8, -3.4, -2.2), permeability=(-5.4, -6.2, -4.7), herg=(0.72, 151), ames=(0.05, 310), ld50=(2.6, 2.0, 3.2)),
    4: dict(solubility=(-3.6, -4.4, -2.9), permeability=(-4.7, -5.5, -4.0), herg=(0.64, 180), ames=(0.09, 350), ld50=(2.2, 1.6, 2.9)),
    5: dict(solubility=(-2.2, -2.8, -1.7), permeability=(-4.4, -5.0, -3.8), herg=(0.22, 240), ames=(0.15, 290), ld50=(2.9, 2.3, 3.5)),
    6: dict(solubility=(-4.1, -5.2, -3.0), permeability=(-5.0, -5.8, -4.2), herg=(0.26, 120), ames=(0.10, 260), ld50=(2.0, 1.3, 2.8)),
    7: dict(solubility=(-3.9, -4.7, -3.1), permeability=(-4.6, -5.3, -3.9), herg=(0.12, 260), ames=(0.04, 420), ld50=(2.7, 2.1, 3.3)),
    8: dict(solubility=(-0.8, -1.5, -0.1), permeability=(-5.3, -6.4, -4.4), herg=(0.08, 300), ames=(0.03, 440), ld50=(2.3, 1.5, 3.1)),
}
MEASURED = {2: {"solubility": -4.0}, 5: {"solubility": -2.4}}


def svg_for(mol, hl_atoms=None, w=300, h=220):
    mol = Chem.Mol(mol)
    AllChem.Compute2DCoords(mol)
    d = rdMolDraw2D.MolDraw2DSVG(w, h)
    o = d.drawOptions()
    o.padding = 0.08
    o.bondLineWidth = 2
    o.addStereoAnnotation = False
    atoms, bonds, cols = [], [], {}
    if hl_atoms:
        col = (0.55, 0.74, 0.96)
        atoms = list(hl_atoms)
        aset = set(atoms)
        bonds = [b.GetIdx() for b in mol.GetBonds() if b.GetBeginAtomIdx() in aset and b.GetEndAtomIdx() in aset]
        cols = {a: col for a in atoms}
    rdMolDraw2D.PrepareAndDrawMolecule(d, mol, highlightAtoms=atoms, highlightBonds=bonds,
                                       highlightAtomColors=cols,
                                       highlightBondColors={b: (0.55, 0.74, 0.96) for b in bonds})
    d.FinishDrawing()
    s = d.GetDrawingText()
    # width/height attributes would fix size; the viewBox lets CSS scale it
    s = re.sub(r"<\?xml[^>]*\?>\s*", "", s)
    s = re.sub(r"width='\d+px' height='\d+px' viewBox", "viewBox", s, count=1)
    return s


def conformer_molblock(mol):
    m = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = 7
    if AllChem.EmbedMolecule(m, params) != 0:
        params.useRandomCoords = True
        AllChem.EmbedMolecule(m, params)
    AllChem.MMFFOptimizeMolecule(m, maxIters=2000)
    return Chem.MolToMolBlock(m)


def shares(rng):
    a = rng.uniform(0.35, 0.55)
    b = rng.uniform(0.2, 0.4)
    a, b = round(a, 2), round(b, 2)
    return a, b, round(1 - a - b, 2)


def build_project(out: Path):
    gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    train = []
    (out / "fixtures" / "svg").mkdir(parents=True, exist_ok=True)
    for k, smi in enumerate(TRAIN_SMILES, 1):
        m = Chem.MolFromSmiles(smi)
        name = f"train_{k:02d}"
        (out / "fixtures" / "svg" / f"{name}.svg").write_text(svg_for(m, w=200, h=150))
        train.append(dict(id=name, smiles=smi, svg=f"fixtures/svg/{name}.svg",
                          conformer_molblock=conformer_molblock(m), mol=m, fp=gen.GetFingerprint(m)))
    rng = random.Random(20261103)
    compounds = []
    for cid, smi, fname, smarts in COMPOUNDS:
        m = Chem.MolFromSmiles(smi)
        patt = Chem.MolFromSmarts(smarts)
        match = m.GetSubstructMatch(patt)
        assert match, (cid, smarts)
        (out / "fixtures" / "svg" / f"c{cid}.svg").write_text(svg_for(m))
        (out / "fixtures" / "svg" / f"c{cid}_hl.svg").write_text(svg_for(m, match))
        fp = gen.GetFingerprint(m)
        sims = sorted(((DataStructs.TanimotoSimilarity(fp, t["fp"]), t["id"]) for t in train), reverse=True)
        sims = [s for s in sims if s[0] < 0.999][:3]
        preds = {}
        for ep, info in ENDPOINTS.items():
            v = VALUES[cid][ep]
            a, b, c = shares(rng)
            frag_share = round(rng.uniform(0.2, 0.5), 2)
            n_ens = 10 if (cid == 3 and ep == "herg") else 5
            p = dict(endpoint=ep, kind=info["kind"], model_version_id=FIXTURE_UUID, ensemble_n=n_ens,
                     coverage=0.9, probability=None, interval_low=None, interval_high=None)
            if info["kind"] == "regression":
                p.update(value=v[0], interval_low=v[1], interval_high=v[2], sample_size=info["cal_n"],
                         method_note=("Ensemble of %d Chemprop models; split conformal interval from a scaffold-split "
                                      "calibration set (n=%d), width scaled by similarity to training. FIXTURE values." % (n_ens, info["cal_n"])),
                         calibration=dict(set_n=info["cal_n"], median_width=info["cal_width"]))
            else:
                p.update(value=v[0], probability=v[0], sample_size=v[1], coverage=0.9,
                         method_note=("Ensemble mean of %d Chemprop models, isotonic recalibration on a scaffold-split "
                                      "calibration set (n=%d). FIXTURE values." % (n_ens, info["cal_n"])),
                         calibration=dict(set_n=info["cal_n"], n_near=v[1],
                                          observed_fraction_near=0.74 if (cid == 3 and ep == "herg") else round(min(0.97, v[0] + rng.uniform(-0.05, 0.05)), 2)))
            if cid == 3 and ep == "herg":
                a, b, c = 0.46, 0.38, 0.16
                ranges = [[0.38, 0.55], [0.33, 0.43], [0.12, 0.20]]
                frag_share = 0.38
            else:
                ranges = [[round(max(0, x - 0.07), 2), round(min(1, x + 0.08), 2)] for x in (a, b, c)]
            p["breakdown"] = dict(ensemble_disagreement=a, similarity_to_training=b, assay_noise=c, ranges=ranges)
            p["fragment_share_of_disagreement"] = frag_share
            preds[ep] = p
        compounds.append(dict(
            id=cid, name=f"Compound {cid}", smiles=smi, svg=f"fixtures/svg/c{cid}.svg",
            svg_highlight=f"fixtures/svg/c{cid}_hl.svg",
            fragment=dict(name=fname, smarts=smarts, atom_indices=list(match),
                          training_count=12 if cid == 3 else 30 + 17 * cid, training_total=8400),
            conformer=dict(molblock=conformer_molblock(m), method=CONFORMER_METHOD, label=CONFORMER_LABEL),
            measured=MEASURED.get(cid, {}),
            predictions=preds,
            nearest=[dict(train_id=tid, tanimoto=round(s, 2),
                          measured_herg_positive=(int(s * 100) + cid) % 2 == 0,
                          measured_ames_positive=False) for s, tid in sims],
        ))
    project = dict(
        fixture=True,
        fixture_label="FIXTURE data: illustrative layout content, not model predictions.",
        project_name="Series A analogs", tier="free", ensemble_n=5, coverage=0.9,
        endpoints=ENDPOINTS,
        ranking_default=dict(
            lam=0.25,
            weights=[dict(endpoint="solubility", weight=8, higher_is_better=True),
                     dict(endpoint="permeability", weight=5, higher_is_better=True),
                     dict(endpoint="herg", weight=9, higher_is_better=False),
                     dict(endpoint="ames", weight=7, higher_is_better=False),
                     dict(endpoint="ld50", weight=3, higher_is_better=True)],
            limits=[dict(endpoint="herg", min=None, max=0.30, use_unfavorable_bound=False),
                    dict(endpoint="ames", min=None, max=0.20, use_unfavorable_bound=False),
                    dict(endpoint="solubility", min=-4.5, max=None, use_unfavorable_bound=False)]),
        compounds=compounds,
        conformer_label=CONFORMER_LABEL,
    )
    (out / "fixtures" / "project.json").write_text(json.dumps(project, indent=1))
    training = [{k: v for k, v in t.items() if k not in ("mol", "fp")} for t in train]
    (out / "fixtures" / "training.json").write_text(json.dumps(
        dict(fixture=True, note="Stand-in compounds for layout. Not the real training set; measured labels are fixture values.",
             compounds=training), indent=1))


def build_logo(out: Path):
    (out / "assets").mkdir(exist_ok=True)
    m = Chem.MolFromSmiles("c1ccccc1")
    d = rdMolDraw2D.MolDraw2DSVG(128, 128)
    o = d.drawOptions()
    o.clearBackground = False
    o.bondLineWidth = 5
    o.padding = 0.12
    o.updateAtomPalette({6: (0.90, 0.91, 0.93)})
    d.DrawMolecule(m)
    d.FinishDrawing()
    s = re.sub(r"<\?xml[^>]*\?>\s*", "", d.GetDrawingText())
    s = re.sub(r"width='\d+px' height='\d+px' viewBox", "viewBox", s, count=1)
    s = s.replace("<svg ", "<svg role='img' aria-label='Metis logo: benzene ring' ", 1)
    (out / "assets" / "benzene.svg").write_text(s)
    # erlotinib 2D fallback on charcoal
    e = Chem.MolFromSmiles("COCCOc1cc2ncnc(Nc3cccc(C#C)c3)c2cc1OCCOC")
    AllChem.Compute2DCoords(e)
    d = rdMolDraw2D.MolDraw2DSVG(480, 360)
    o = d.drawOptions()
    o.clearBackground = False
    o.bondLineWidth = 3
    o.updateAtomPalette({6: (0.90, 0.91, 0.93), 7: (0.42, 0.65, 0.90), 8: (0.93, 0.52, 0.44)})
    d.DrawMolecule(e)
    d.FinishDrawing()
    s = re.sub(r"<\?xml[^>]*\?>\s*", "", d.GetDrawingText())
    s = re.sub(r"width='\d+px' height='\d+px' viewBox", "viewBox", s, count=1)
    (out / "assets" / "erlotinib.svg").write_text(s)


def build_structures(out: Path, pdb: Path, sdf: Path):
    d = out / "fixtures" / "structures"
    d.mkdir(parents=True, exist_ok=True)
    lig = Chem.MolFromMolFile(str(sdf), removeHs=False)
    conf = lig.GetConformer()
    lig_xyz = [(conf.GetAtomPosition(i).x, conf.GetAtomPosition(i).y, conf.GetAtomPosition(i).z) for i in range(lig.GetNumAtoms())]
    lines = [l for l in pdb.read_text().splitlines() if l.startswith("ATOM") and l[21] == "A"]
    res = {}
    for l in lines:
        res.setdefault(int(l[22:26]), []).append(l)

    def near(l, cutoff):
        x, y, z = float(l[30:38]), float(l[38:46]), float(l[46:54])
        return any((x - a) ** 2 + (y - b) ** 2 + (z - c) ** 2 < cutoff ** 2 for a, b, c in lig_xyz)
    shell = sorted(r for r, ls in res.items() if any(near(l, 6.0) for l in ls))
    keep = []
    for r in sorted(res):
        for l in res[r]:
            if r in shell or l[12:16].strip() in ("N", "CA", "C", "O"):
                keep.append(l)
    hdr = ("REMARK   Trimmed from RCSB 1M17 chain A (EGFR kinase domain). Backbone atoms everywhere, all atoms for the\n"
           "REMARK   %d residues within 6 A of erlotinib (pocket shell). Waters and other ligands removed.\n"
           "REMARK   Pocket shell residues: %s\n" % (len(shell), " ".join(map(str, shell))))
    (d / "1m17_chainA_pocket.pdb").write_text(hdr + "\n".join(keep) + "\nEND\n")
    (d / "erlotinib_1m17.sdf").write_text(Chem.MolToMolBlock(lig) + "$$$$\n")
    cx = [sum(p[i] for p in lig_xyz) / len(lig_xyz) for i in range(3)]
    meta = dict(pdb_id="1M17", ligand="erlotinib (AQ4)", pocket_shell_residues=shell, ligand_centroid=[round(x, 2) for x in cx],
                kept_atom_lines=len(keep))
    (d / "1m17_meta.json").write_text(json.dumps(meta, indent=1))
    return meta


def md_to_html(md: str, title_fallback: str) -> tuple[str, str]:
    out, in_list, title = [], None, title_fallback

    def inline(s):
        s = html.escape(s)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    for raw in md.splitlines():
        line = raw.rstrip()
        m_num, m_bul = re.match(r"^\d+\.\s+(.*)", line), re.match(r"^[-*]\s+(.*)", line)
        if in_list and not (m_num or m_bul):
            out.append(f"</{in_list}>")
            in_list = None
        if line.startswith("# "):
            title = line[2:]
            out.append(f"<h1>{inline(title)}</h1>")
        elif m_num or m_bul:
            tag = "ol" if m_num else "ul"
            if in_list != tag:
                if in_list:
                    out.append(f"</{in_list}>")
                out.append(f"<{tag}>")
                in_list = tag
            out.append(f"<li>{inline((m_num or m_bul).group(1))}</li>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    if in_list:
        out.append(f"</{in_list}>")
    return title, "\n".join(out)


def build_legal(out: Path):
    (out / "legal").mkdir(exist_ok=True)
    names = {"TERMS_OF_SERVICE": "terms", "PRIVACY_POLICY": "privacy", "DISCLAIMER": "disclaimer"}
    disclaimer = ""
    for src, slug in names.items():
        md = (ROOT / "docs" / "legal" / f"{src}.md").read_text()
        title, body = md_to_html(md, slug)
        if slug == "disclaimer":
            disclaimer = " ".join(l for l in md.splitlines() if l.strip() and not l.startswith("#"))
        page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title.split(' — ')[0])} | Metis</title>
<link rel="stylesheet" href="../css/app.css"></head>
<body class="legal-page"><header class="appbar"><a class="brand" href="../index.html">Metis</a><span class="crumb">Legal / {html.escape(title.split(' — ')[0])}</span></header>
<main class="legal-body">
<p class="draft-note" role="note"><strong>DRAFT.</strong> This is a draft rendered from docs/legal. It is not legal advice and has not had lawyer review.</p>
{body}
<p><a href="../auth.html">Back to sign up</a></p>
</main></body></html>
"""
        (out / "legal" / f"{slug}.html").write_text(page)
    (out / "js").mkdir(exist_ok=True)
    (out / "js" / "legal_text.js").write_text(
        "// Generated by scripts/make_depictions.py from docs/legal/DISCLAIMER.md. Do not edit.\n"
        f"export const DISCLAIMER = {json.dumps(disclaimer)};\n")


MODEL_DEFS = {
    # id: label, unit, kind, datasets, extra baselines, limitation text
    "herg": ("hERG blockade", "P(blocker)", "classification", ["hERG_Karim"], [],
             "Mixed assay conditions across sources, class balance, and which scaffolds are missing from the data."),
    "ames": ("AMES mutagenicity", "P(mutagenic)", "classification", ["AMES"], [],
             "Bacterial assay only; strain and metabolic-activation differences between sources are not modelled."),
    "dili": ("Drug-induced liver injury", "P(injury)", "classification", ["DILI"], [],
             "Small dataset; labels come from heterogeneous clinical and literature sources."),
    "ld50": ("Acute toxicity LD50 (rodent)", "log mmol/kg", "regression", ["LD50_Zhu"], [],
             "Species, route and strain differ between records; wide noise ceiling."),
    "solubility": ("Solubility (thermodynamic)", "logS", "regression", ["Solubility_AqSolDB"], ["GSE equation (baseline for solubility)"],
                   "Thermodynamic solubility only; kinetic solubility is pending suitable data. Temperature and pH conditions vary by source."),
    "permeability": ("Permeability (Caco-2)", "log cm/s", "regression", ["Caco2_Wang", "PAMPA_NCATS"], [],
                     "Cell-based and artificial-membrane assays are different measurements and are not pooled."),
    "logd": ("logD at pH 7.4", "logD", "regression", ["Lipophilicity_AstraZeneca"], ["Crippen logP (baseline for logP)"],
             "The TDC Lipophilicity set is logD at pH 7.4, not logP; it is labelled as logD everywhere."),
    "half_life": ("Half-life (human)", "log h", "regression", ["Half_Life_Obach"], [],
                  "Small dataset; in vivo values depend on dose and population."),
    "clearance": ("Clearance (microsomal, hepatocyte)", "log mL/min/g", "regression", ["Clearance_Microsome_AZ", "Clearance_Hepatocyte_AZ"], [],
                  "Two in vitro systems with different scales; not interchangeable."),
}


def build_models(out: Path):
    models = {}
    for mid, (label, unit, kind, dsets, extra, lim) in MODEL_DEFS.items():
        cls = kind == "classification"
        metrics = ["AUROC", "MCC", "AUPRC"] if cls else ["RMSE", "MAE", "R2"]
        rows = [dict(model="Random forest, Morgan fingerprints (baseline)", values=[0.0] * 3, note="reference")]
        rows += [dict(model=b, values=[0.0] * 3, note="reference") for b in extra]
        rows.append(dict(model="Metis ensemble (N = 10)", values=[0.0] * 3, note="placeholder: comparison not yet computed"))
        models[mid] = dict(
            id=mid, label=label, unit=unit, kind=kind,
            datasets=[dict(name=d, source="Therapeutics Data Commons (credit TDC and the original authors)", n="n = (placeholder)",
                           license="status pending: written answer on TDC licensing required (gate G1)") for d in dsets],
            split="Scaffold (time split where dates exist). Never random-only.",
            calibration_n="(placeholder)", target_coverage=0.9, heldout_coverage="(placeholder)",
            metrics=metrics, benchmark=rows,
            ensemble_table=[dict(n=n, coverage="0.0%", runtime="0 s") for n in (3, 5, 10, 20)],
            limitations=lim,
            attribution_check="Attribution check: error for compounds containing a flagged fragment vs. without it (result goes here).",
            examples=["train_01", "train_05", "train_10"],
        )
    (out / "fixtures" / "models.json").write_text(json.dumps(dict(
        fixture=True, note="FIXTURE data: all numbers are placeholders (zeros), not benchmark results.", models=models), indent=1))


def build_docking(out: Path, meta):
    dock = dict(
        fixture=True, note="FIXTURE data: layout content only; no docking or site detection was run.",
        pdb_id="1M17", method="X-ray diffraction", resolution="2.6 A", title="EGFR kinase domain with erlotinib",
        other_methods=["No other experimental method listed in this fixture"],
        box=dict(center=meta["ligand_centroid"], size=[20, 20, 20]),
        pocket_shell_residues=meta["pocket_shell_residues"],
        sites=[dict(source="You picked", site="residues 719 to 726 (placeholder)", score="n/a", residues=[719, 720, 721, 722, 723, 724, 725, 726]),
               dict(source="Detected", site="pocket 1 (placeholder)", score="0.81", residues=meta["pocket_shell_residues"][:12]),
               dict(source="Literature", site="cited site (link placeholder)", score="2 papers", residues=[769, 771])],
        change_log=["14 waters removed (placeholder count)", "2 residues protonated differently from the file (placeholder)", "3 atoms added to residue 87 (placeholder)"],
    )
    (out / "fixtures" / "docking.json").write_text(json.dumps(dock, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--egfr-pdb", default=DEF_PDB)
    ap.add_argument("--egfr-sdf", default=DEF_SDF)
    a = ap.parse_args()
    build_logo(WEB)
    build_project(WEB)
    meta = build_structures(WEB, Path(a.egfr_pdb), Path(a.egfr_sdf))
    build_legal(WEB)
    build_models(WEB)
    build_docking(WEB, meta)
    print("ok; pocket shell residues:", len(meta["pocket_shell_residues"]), "atom lines:", meta["kept_atom_lines"])


if __name__ == "__main__":
    main()
