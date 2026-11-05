"""Ligand preparation: standardize, protonate at pH (rule-based), ETKDG+MMFF conformers, PDBQT + SDF.

Protonation method: a small SMARTS table of approximate pKa values (documented below). It is NOT a pKa
predictor; the exact table and the pH used are written to the change log so the step is reproducible.
Open Babel (GPL-2.0) and Dimorphite-DL (Apache-2.0) are possible replacements and are not bundled.
"""

from __future__ import annotations

import io
from typing import Any

from rdkit import Chem, RDLogger
from rdkit.Chem import AllChem, Descriptors, rdMolDescriptors, rdPartialCharges
from rdkit.Chem.MolStandardize import rdMolStandardize

from .errors import DockprepError
from .limits import MAX_CONFORMERS, MAX_LIGAND_HEAVY_ATOMS, MAX_SMILES_LEN, sanitize_name
from .pdbqt import pdbqt_atom_line

RDLogger.DisableLog("rdApp.*")

# (name, SMARTS of the ionisable atom, kind, approximate pKa, formal charge change when ionised)
# acids: deprotonated (charge -1) when pH > pKa. bases: protonated (charge +1) when pH < pKa.
IONISATION_RULES = [
    ("sulfonic acid", "[SX4](=O)(=O)[OX2H1]", "acid", -1.0),
    ("phosphate/phosphonate", "[PX4](=O)[OX2H1]", "acid", 2.0),
    ("carboxylic acid", "[CX3](=O)[OX2H1]", "acid", 4.2),
    ("tetrazole", "c1nnn[nH]1", "acid_nh", 4.9),
    ("acyl sulfonamide", "[CX3](=O)[NX3H1][SX4](=O)=O", "acid_nh", 4.5),
    (
        "amidine/guanidine",
        "[NX3;!$(N-C=O)][CX3;!$(C-[#6]=O)]=[NX2;!$(N-[#6]=O);!$(N-a);!$(N-S);!r]",
        "base_amidine",
        12.0,
    ),
    ("aliphatic amine", "[NX3;H2,H1,H0;!$(N-[!#6;!#1]);!$(N-[#6;!X4]);!$([N]-C#N)]", "base", 9.8),
    ("imidazole", "[nX2;r5;$(n1cc[nH]c1),$(n1c[nH]cc1)]", "base_arom", 6.9),
]
_PATTERNS = [(n, Chem.MolFromSmarts(s), k, p) for n, s, k, p in IONISATION_RULES]


class LigandLog:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def add(self, step: str, action: str, detail: str = "", **params: Any) -> None:
        self.entries.append({"step": step, "action": action, "detail": detail, "params": params})


def _load(smiles: str | None, sdf: str | None, log: LigandLog) -> Chem.Mol:
    if smiles:
        if len(smiles) > MAX_SMILES_LEN:
            raise DockprepError("bad_ligand", "SMILES is too long.", 413)
        mol = Chem.MolFromSmiles(smiles.strip())
        log.add("parse", "read SMILES", "input parsed with RDKit", format="smiles")
    elif sdf:
        suppl = Chem.SDMolSupplier()
        suppl.SetData(sdf)
        mols = [m for m in suppl if m is not None][:2]
        if len(mols) > 1:
            raise DockprepError(
                "bad_ligand", "Submit one ligand per request (v1 prepares one ligand at a time).", 422
            )
        mol = mols[0] if mols else None
        log.add("parse", "read SDF record", "input parsed with RDKit", format="sdf")
    else:
        raise DockprepError("bad_ligand", "Provide a SMILES string or an SDF record.", 422)
    if mol is None:
        raise DockprepError("bad_ligand", "Ligand could not be parsed.", 422)
    if mol.GetNumHeavyAtoms() == 0 or mol.GetNumHeavyAtoms() > MAX_LIGAND_HEAVY_ATOMS:
        raise DockprepError("bad_ligand", f"Ligand must have 1 to {MAX_LIGAND_HEAVY_ATOMS} heavy atoms.", 422)
    return mol


def standardize(mol: Chem.Mol, log: LigandLog) -> Chem.Mol:
    mol = Chem.RemoveHs(mol)
    mol = rdMolStandardize.Cleanup(mol)
    log.add("standardize", "RDKit Cleanup", "normalise functional groups, reionise, remove explicit Hs")
    frags = Chem.GetMolFrags(mol, asMols=True)
    if len(frags) > 1:
        from rdkit.Chem.MolStandardize import rdMolStandardize as S

        keep = S.LargestFragmentChooser().choose(mol)
        dropped = [Chem.MolToSmiles(f) for f in frags if Chem.MolToSmiles(f) != Chem.MolToSmiles(keep)]
        log.add(
            "standardize",
            "removed counter-ions/salt fragments",
            f"kept largest fragment; removed {len(dropped)} fragment(s)",
            removed_fragments=dropped,
            kept_heavy_atoms=keep.GetNumHeavyAtoms(),
        )
        mol = keep
    else:
        log.add("standardize", "fragments", "single fragment, nothing removed")
    un = rdMolStandardize.Uncharger(canonicalOrder=True)
    before = sum(abs(a.GetFormalCharge()) for a in mol.GetAtoms())
    mol = un.uncharge(mol)
    after = sum(abs(a.GetFormalCharge()) for a in mol.GetAtoms())
    log.add(
        "standardize",
        "neutralise charges",
        f"sum |formal charge| {before} -> {after}; ionisation is reassigned at the chosen pH below",
    )
    te = rdMolStandardize.TautomerEnumerator()
    te.SetMaxTautomers(200)
    te.SetRemoveSp3Stereo(False)  # keep specified tetrahedral stereo through tautomer canonicalisation
    te.SetRemoveBondStereo(False)
    try:
        t = te.Canonicalize(mol)
        changed = Chem.MolToSmiles(t) != Chem.MolToSmiles(mol)
        log.add(
            "standardize",
            "canonical tautomer",
            "tautomer changed" if changed else "tautomer unchanged",
            changed=changed,
            method="RDKit TautomerEnumerator.Canonicalize (max 200 tautomers)",
        )
        mol = t
    except Exception:  # noqa: BLE001 - RDKit raises varied errors; keep input tautomer
        log.add("standardize", "canonical tautomer", "failed; input tautomer kept")
    Chem.AssignStereochemistry(mol, cleanIt=True, force=True)
    centers = Chem.FindMolChiralCenters(mol, includeUnassigned=True, useLegacyImplementation=False)
    unassigned = [i for i, lab in centers if lab == "?"]
    log.add(
        "stereo",
        "stereocentres",
        f"{len(centers)} stereocentre(s), {len(unassigned)} unspecified",
        specified=len(centers) - len(unassigned),
        unspecified_atom_indices=unassigned,
        note="unspecified centres are embedded with an arbitrary configuration" if unassigned else "",
    )
    return mol


def protonate(mol: Chem.Mol, ph: float, log: LigandLog) -> Chem.Mol:
    mol = Chem.RWMol(mol)
    changed: list[str] = []
    used: set[int] = set()
    for name, patt, kind, pka in _PATTERNS:
        for match in mol.GetSubstructMatches(patt):
            if kind == "acid" and ph > pka:
                idx = match[-1]
                if idx in used:
                    continue
                a = mol.GetAtomWithIdx(idx)
                a.SetFormalCharge(-1)
                a.SetNumExplicitHs(0)
                a.SetNoImplicit(True)
                used.add(idx)
                changed.append(f"{name}: deprotonated atom {idx} (pKa~{pka}, pH {ph})")
            elif kind == "acid_nh" and ph > pka:
                nh = [
                    i
                    for i in match
                    if mol.GetAtomWithIdx(i).GetSymbol() == "N" and mol.GetAtomWithIdx(i).GetTotalNumHs() > 0
                ]
                if nh and nh[0] not in used:
                    a = mol.GetAtomWithIdx(nh[0])
                    a.SetFormalCharge(-1)
                    a.SetNumExplicitHs(0)
                    a.SetNoImplicit(True)
                    used.add(nh[0])
                    changed.append(f"{name}: deprotonated atom {nh[0]} (pKa~{pka}, pH {ph})")
            elif kind == "base" and ph < pka:
                idx = match[0]
                a = mol.GetAtomWithIdx(idx)
                if idx in used or a.GetIsAromatic() or a.GetFormalCharge() != 0:
                    continue
                a.SetFormalCharge(1)
                a.SetNumExplicitHs(a.GetTotalNumHs() + 1)
                a.SetNoImplicit(True)
                used.add(idx)
                changed.append(f"{name}: protonated atom {idx} (pKa~{pka}, pH {ph})")
            elif kind == "base_amidine" and ph < pka:
                idx = match[-1]
                a = mol.GetAtomWithIdx(idx)
                if idx in used or a.GetFormalCharge() != 0:
                    continue
                a.SetFormalCharge(1)
                a.SetNumExplicitHs(a.GetTotalNumHs() + 1)
                a.SetNoImplicit(True)
                used.add(idx)
                changed.append(f"{name}: protonated atom {idx} (pKa~{pka}, pH {ph})")
            elif kind == "base_arom" and ph < pka:
                idx = match[0]
                a = mol.GetAtomWithIdx(idx)
                if idx in used:
                    continue
                a.SetFormalCharge(1)
                a.SetNumExplicitHs(1)
                a.SetNoImplicit(True)
                used.add(idx)
                changed.append(f"{name}: protonated atom {idx} (pKa~{pka}, pH {ph})")
    out = mol.GetMol()
    try:
        Chem.SanitizeMol(out)
    except Exception:  # noqa: BLE001
        raise DockprepError("bad_ligand", "Ligand could not be protonated consistently.", 422) from None
    log.add(
        "protonate",
        "ionisation at pH",
        f"{len(changed)} site(s) changed",
        ph=ph,
        changes=changed,
        method="Metis rule table (RDKit SMARTS, approximate pKa); not a pKa prediction",
        rules=[{"name": n, "kind": k, "approx_pKa": p} for n, _, k, p in _PATTERNS],
    )
    return out


def embed(mol: Chem.Mol, n_conf: int, seed: int, log: LigandLog) -> tuple[Chem.Mol, list[float], str]:
    molh = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = seed
    params.useRandomCoords = False
    cids = list(AllChem.EmbedMultipleConfs(molh, numConfs=n_conf, params=params))
    if not cids:
        params.useRandomCoords = True
        cids = list(AllChem.EmbedMultipleConfs(molh, numConfs=n_conf, params=params))
    if not cids:
        raise DockprepError("bad_ligand", "3D conformer generation failed.", 422)
    method = "ETKDGv3 + MMFF94s"
    try:
        res = AllChem.MMFFOptimizeMoleculeConfs(molh, maxIters=2000, mmffVariant="MMFF94s")
        energies = [e for _, e in res]
        if not AllChem.MMFFHasAllMoleculeParams(molh):
            raise ValueError
    except Exception:  # noqa: BLE001
        res = AllChem.UFFOptimizeMoleculeConfs(molh, maxIters=2000)
        energies = [e for _, e in res]
        method = "ETKDGv3 + UFF (MMFF parameters unavailable)"
    log.add(
        "conformers",
        "generate and minimise",
        f"{len(cids)} conformer(s) kept",
        method=method,
        n_requested=n_conf,
        seed=seed,
        energies_kcal_mol=[round(e, 3) for e in energies],
        energy_note="MMFF energies are force-field values, not binding energies",
    )
    return molh, energies, method


# --------- PDBQT ----------
_ROT = Chem.MolFromSmarts("[!D1&!$(*#*)]-&!@[!D1&!$(*#*)]")
_AMIDE = Chem.MolFromSmarts("[CX3](=[O,S])-&!@[NX3]")


def _ad_type(a: Chem.Atom) -> str:
    s = a.GetSymbol()
    if s == "C":
        return "A" if a.GetIsAromatic() else "C"
    if s == "N":
        if a.GetIsAromatic() and a.GetTotalNumHs() == 0 and a.GetDegree() == 2 and a.GetFormalCharge() == 0:
            return "NA"
        if a.GetDegree() == 1 and a.GetTotalNumHs() == 0:  # nitrile N
            return "NA"
        return "N"
    if s == "O":
        return "OA"
    if s == "S":
        return "SA" if a.GetIsAromatic() else "S"
    if s == "H":
        return "HD"
    return {"F": "F", "Cl": "Cl", "Br": "Br", "I": "I", "P": "P", "B": "B"}.get(s, s[:2])


def write_ligand_pdbqt(molh: Chem.Mol, conf_id: int, name: str, log: LigandLog) -> str:
    rdPartialCharges.ComputeGasteigerCharges(molh)
    conf = molh.GetConformer(conf_id)
    n = molh.GetNumAtoms()
    heavy = [a.GetIdx() for a in molh.GetAtoms() if a.GetAtomicNum() > 1]
    # polar H kept (on N/O/S), nonpolar H merged into carbon: charge moves to heavy atom
    keep_h: dict[int, int] = {}
    charge = {i: float(molh.GetAtomWithIdx(i).GetDoubleProp("_GasteigerCharge")) for i in range(n)}
    for a in molh.GetAtoms():
        if a.GetAtomicNum() == 1:
            nb = a.GetNeighbors()[0]
            if nb.GetSymbol() in ("N", "O", "S"):
                keep_h[a.GetIdx()] = nb.GetIdx()
            else:
                charge[nb.GetIdx()] += charge[a.GetIdx()]
    for k in list(charge):
        if charge[k] != charge[k]:
            charge[k] = 0.0
    # rotatable bonds on the heavy-atom graph
    heavy_set = set(heavy)
    amide = {tuple(sorted((m[0], m[2]))) for m in molh.GetSubstructMatches(_AMIDE)}
    rot: set[tuple[int, int]] = set()
    for b, e in molh.GetSubstructMatches(_ROT):
        if b in heavy_set and e in heavy_set and not molh.GetBondBetweenAtoms(b, e).IsInRing():
            if tuple(sorted((b, e))) in amide:
                continue
            ab, ae = molh.GetAtomWithIdx(b), molh.GetAtomWithIdx(e)
            if (
                sum(1 for x in ab.GetNeighbors() if x.GetAtomicNum() > 1) < 2
                or sum(1 for x in ae.GetNeighbors() if x.GetAtomicNum() > 1) < 2
            ):
                continue
            rot.add(tuple(sorted((b, e))))
    # fragments = connected components of heavy atoms after removing rotatable bonds
    frag_of: dict[int, int] = {}
    frags: list[list[int]] = []
    for s in heavy:
        if s in frag_of:
            continue
        fid, stack, comp = len(frags), [s], []
        frag_of[s] = fid
        while stack:
            u = stack.pop()
            comp.append(u)
            for nb in molh.GetAtomWithIdx(u).GetNeighbors():
                v = nb.GetIdx()
                if v in heavy_set and v not in frag_of and tuple(sorted((u, v))) not in rot:
                    frag_of[v] = fid
                    stack.append(v)
        frags.append(comp)
    hs_of: dict[int, list[int]] = {}
    for h, p in keep_h.items():
        hs_of.setdefault(p, []).append(h)
    root = max(range(len(frags)), key=lambda i: len(frags[i]))
    lines: list[str] = []
    serial_of: dict[int, int] = {}
    counter = [0]
    sname = sanitize_name(name, "LIG")

    def atom_line(i: int) -> None:
        counter[0] += 1
        serial_of[i] = counter[0]
        a = molh.GetAtomWithIdx(i)
        p = conf.GetAtomPosition(i)
        el = a.GetSymbol()
        lines.append(
            pdbqt_atom_line(
                counter[0],
                f"{el}{counter[0]}"[:4],
                "LIG",
                "A",
                1,
                (p.x, p.y, p.z),
                charge[i],
                _ad_type(a),
                "HETATM",
            )
        )

    def emit(fid: int, entry: int | None) -> None:
        order = frags[fid][:]
        if entry is not None:
            order.remove(entry)
            order.insert(0, entry)
        for i in order:
            atom_line(i)
            for h in hs_of.get(i, []):
                atom_line(h)
        for i in order:
            for nb in molh.GetAtomWithIdx(i).GetNeighbors():
                j = nb.GetIdx()
                if (
                    j in heavy_set
                    and frag_of[j] != fid
                    and tuple(sorted((i, j))) in rot
                    and frag_of[j] not in done
                ):
                    done.add(frag_of[j])
                    # serial of child's entry atom is the next serial to be assigned
                    lines.append(f"BRANCH {serial_of[i]:4d} {counter[0] + 1:4d}")
                    mark = len(lines) - 1
                    emit(frag_of[j], j)
                    lines.append(f"ENDBRANCH {serial_of[i]:4d} {serial_of[j]:4d}")
                    lines[mark] = f"BRANCH {serial_of[i]:4d} {serial_of[j]:4d}"

    done = {root}
    lines.append("ROOT")
    # root atoms (no branches inside ROOT block): emit root fragment atoms first, then branches
    order = frags[root]
    for i in order:
        atom_line(i)
        for h in hs_of.get(i, []):
            atom_line(h)
    lines.append("ENDROOT")
    for i in order:
        for nb in molh.GetAtomWithIdx(i).GetNeighbors():
            j = nb.GetIdx()
            if (
                j in heavy_set
                and frag_of[j] != root
                and tuple(sorted((i, j))) in rot
                and frag_of[j] not in done
            ):
                done.add(frag_of[j])
                mark = len(lines)
                lines.append("")
                emit(frag_of[j], j)
                lines.append(f"ENDBRANCH {serial_of[i]:4d} {serial_of[j]:4d}")
                lines[mark] = f"BRANCH {serial_of[i]:4d} {serial_of[j]:4d}"
    header = [
        f"REMARK  Name = {sname}",
        f"REMARK  {len(rot)} active torsions",
        "REMARK  Prepared by Metis dockprep (rule-based; Gasteiger charges; nonpolar H merged)",
    ]
    out = "\n".join(header + lines + [f"TORSDOF {len(rot)}"]) + "\n"
    log.add(
        "pdbqt",
        "write ligand PDBQT",
        f"{len([ln for ln in lines if ln.startswith(('ATOM', 'HETATM'))])} atoms, {len(rot)} torsions",
        charges="RDKit Gasteiger",
        atom_types="simplified AutoDock typing (C/A/N/NA/OA/SA/HD/halogens)",
        nonpolar_hydrogens="merged into carbon",
    )
    return out


def prepare_ligand(
    smiles: str | None = None,
    sdf: str | None = None,
    ph: float = 7.4,
    n_conformers: int = 10,
    seed: int = 42,
    name: str = "ligand",
    all_conformers_sdf: bool = False,
) -> dict:
    if not (0.0 <= ph <= 14.0):
        raise DockprepError("bad_param", "pH must be between 0 and 14.", 422)
    if not (1 <= n_conformers <= MAX_CONFORMERS):
        raise DockprepError("bad_param", f"Conformers must be between 1 and {MAX_CONFORMERS}.", 422)
    name = sanitize_name(name, "ligand")
    log = LigandLog()
    mol = _load(smiles, sdf, log)
    mol = standardize(mol, log)
    mol = protonate(mol, ph, log)
    molh, energies, method = embed(mol, n_conformers, seed, log)
    best = min(range(len(energies)), key=lambda i: energies[i])
    cid = molh.GetConformers()[best].GetId()
    log.add(
        "conformers",
        "selected conformer for docking input",
        "lowest force-field energy",
        conformer_index=best,
    )
    pdbqt = write_ligand_pdbqt(molh, cid, name, log)
    buf = io.StringIO()
    sw = Chem.SDWriter(buf)
    ids = [c.GetId() for c in molh.GetConformers()] if all_conformers_sdf else [cid]
    for k in ids:
        m = Chem.Mol(molh)
        m.SetProp("_Name", name)
        m.SetProp("MetisConformerMethod", method)
        m.SetProp("MetisPH", str(ph))
        m.SetProp("MetisNote", "computed conformer, not an experimental structure")
        sw.write(m, confId=k)
    sw.close()
    sdf_text = buf.getvalue()
    info = {
        "name": name,
        "ph": ph,
        "conformer_method": method,
        "n_conformers": len(energies),
        "heavy_atoms": molh.GetNumHeavyAtoms(),
        "formula": rdMolDescriptors.CalcMolFormula(molh),
        "mol_weight": round(Descriptors.MolWt(molh), 3),
        "net_charge": Chem.GetFormalCharge(molh),
        "canonical_smiles_prepared": Chem.MolToSmiles(Chem.RemoveHs(molh)),
        "label": "computed conformer, not an experimental structure",
    }
    return {
        "files": {f"{name}.pdbqt": pdbqt, f"{name}.sdf": sdf_text},
        "change_log": log.entries,
        "report": info,
    }
