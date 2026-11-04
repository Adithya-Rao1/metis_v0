# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310
"""Protein preparation. Every change is recorded as a structured change-log entry.

Hydrogens and protonation states: PDB2PQR (BSD-3) + PROPKA (LGPL-2.1) when installed (run as a subprocess in
the sandbox); otherwise a documented fallback that assigns no hydrogens and only records pH-based expected
charge states of titratable residues. The method used is always written to the log and the report.
"""
from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed argv, no shell, timeout
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from .errors import DockprepError
from .limits import SUBPROCESS_TIMEOUT_S, sanitize_name
from .pdbio import STD_AA, WATER, Atom, Structure, parse_pdb, parse_structure, write_pdb
from .pdbqt import pdbqt_atom_line

ADDITIVES = frozenset(
    ["GOL", "EDO", "PEG", "PGE", "PG4", "1PE", "P6G", "SO4", "PO4", "ACT", "ACE", "ACY", "DMS", "FMT", "TRS", "MPD", "BME", "MES", "EPE", "HEZ", "IPA", "NO3", "SCN", "CIT", "TLA", "BOG", "OCT", "DTT", "CL", "NA", "K", "BR", "IOD", "I"]
)
METALS = frozenset(["ZN", "MG", "MN", "FE", "FE2", "CA", "CU", "CU1", "CO", "NI", "CD", "HG"])
_METAL_TYPES = {"ZN": "Zn", "MG": "Mg", "MN": "Mn", "FE": "Fe", "FE2": "Fe", "CA": "Ca", "CU": "Cu", "CU1": "Cu",
                "CO": "Co", "NI": "Ni", "CD": "Cd", "HG": "Hg"}

_BB = {"N", "CA", "C", "O"}
EXPECTED_HEAVY = {
    "GLY": _BB, "ALA": _BB | {"CB"}, "SER": _BB | {"CB", "OG"}, "CYS": _BB | {"CB", "SG"}, "VAL": _BB | {"CB", "CG1", "CG2"},
    "THR": _BB | {"CB", "OG1", "CG2"}, "LEU": _BB | {"CB", "CG", "CD1", "CD2"}, "ILE": _BB | {"CB", "CG1", "CG2", "CD1"},
    "PRO": _BB | {"CB", "CG", "CD"}, "MET": _BB | {"CB", "CG", "SD", "CE"}, "ASP": _BB | {"CB", "CG", "OD1", "OD2"},
    "ASN": _BB | {"CB", "CG", "OD1", "ND2"}, "GLU": _BB | {"CB", "CG", "CD", "OE1", "OE2"},
    "GLN": _BB | {"CB", "CG", "CD", "OE1", "NE2"}, "LYS": _BB | {"CB", "CG", "CD", "CE", "NZ"},
    "ARG": _BB | {"CB", "CG", "CD", "NE", "CZ", "NH1", "NH2"},
    "HIS": _BB | {"CB", "CG", "ND1", "CD2", "CE1", "NE2"}, "PHE": _BB | {"CB", "CG", "CD1", "CD2", "CE1", "CE2", "CZ"},
    "TYR": _BB | {"CB", "CG", "CD1", "CD2", "CE1", "CE2", "CZ", "OH"},
    "TRP": _BB | {"CB", "CG", "CD1", "CD2", "NE1", "CE2", "CE3", "CZ2", "CZ3", "CH2"},
}
_AROM = {
    "PHE": {"CG", "CD1", "CD2", "CE1", "CE2", "CZ"}, "TYR": {"CG", "CD1", "CD2", "CE1", "CE2", "CZ"},
    "TRP": {"CG", "CD1", "CD2", "CE2", "CE3", "CZ2", "CZ3", "CH2"}, "HIS": {"CG", "CD2", "CE1"},
}
# approximate model pKa values used only by the no-PDB2PQR fallback and for context in logs
TITRATABLE_PKA = {"ASP": 3.9, "GLU": 4.3, "HIS": 6.0, "CYS": 8.3, "TYR": 10.1, "LYS": 10.5, "ARG": 12.5}


class ChangeLog:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def add(self, step: str, action: str, target: str = "", detail: str = "", **params) -> None:
        self.entries.append({"step": step, "action": action, "target": target, "detail": detail, "params": params})


def rid(chain: str, resseq: int, icode: str = "") -> str:
    return f"{chain}:{resseq}{icode}"


def find_pdb2pqr() -> str | None:
    env = os.environ.get("METIS_PDB2PQR")
    if env and Path(env).is_file():
        return env
    cand = Path(sys.executable).parent / "pdb2pqr"
    if cand.is_file():
        return str(cand)
    return shutil.which("pdb2pqr")


def _residues(atoms: list[Atom]) -> dict[tuple, list[Atom]]:
    d: dict[tuple, list[Atom]] = {}
    for a in atoms:
        d.setdefault((a.chain, a.resseq, a.icode, a.resname), []).append(a)
    return d


def resolve_altlocs(atoms: list[Atom], log: ChangeLog) -> list[Atom]:
    by_res: dict[tuple, list[Atom]] = defaultdict(list)
    order: list[tuple] = []
    for a in atoms:
        k = (a.chain, a.resseq, a.icode)
        if k not in by_res:
            order.append(k)
        by_res[k].append(a)
    out: list[Atom] = []
    for k in order:
        ats = by_res[k]
        sums: dict[str, float] = {}
        counts: dict[str, int] = {}
        for a in ats:
            if a.altloc:
                sums[a.altloc] = sums.get(a.altloc, 0.0) + a.occ
                counts[a.altloc] = counts.get(a.altloc, 0) + 1
        if not sums:
            out.extend(ats)
            continue
        labels = {lab: sums[lab] / counts[lab] for lab in sums}                      # MEAN occupancy per label
        keep = max(labels, key=lambda lab: (round(labels[lab], 6), -ord(lab[0])))   # highest mean; tie -> earliest label
        n_labels = {lab: round(v, 2) for lab, v in labels.items()}
        for a in ats:
            if not a.altloc or a.altloc == keep:
                a.altloc = ""
                out.append(a)
        log.add("altloc", "resolved alternate locations", rid(*k) + f" {ats[0].resname}",
                f"kept altloc {keep} (highest mean occupancy); discarded {sorted(set(labels) - {keep})}",
                kept=keep, mean_occupancy_by_altloc=n_labels, policy="highest mean occupancy over the atoms carrying each label, ties to first label")
    return out


def is_het_polymer(a: Atom) -> bool:
    return a.resname in STD_AA or a.resname == "MSE"


def classify_hets(atoms: list[Atom]) -> list[dict]:
    """Return one dict per non-water, non-polymer HETATM residue."""
    groups = _residues([a for a in atoms if a.record == "HETATM" and a.resname not in WATER and not is_het_polymer(a)])
    out = []
    for (chain, seq, ic, name), ats in groups.items():
        heavy = [a for a in ats if not a.is_h]
        if name in METALS or (len(heavy) == 1 and heavy[0].element.upper() in {m.upper() for m in _METAL_TYPES.values()}):
            kind = "metal_ion"
        elif name in ADDITIVES or len(heavy) < 6:
            kind = "additive_or_small"
        else:
            kind = "cocrystal_ligand_candidate"
        out.append({"resname": name, "chain": chain, "resseq": seq, "icode": ic, "kind": kind, "n_heavy": len(heavy), "atoms": ats})
    return out


def detect_missing(atoms: list[Atom], st: Structure, chains: set[str], log: ChangeLog) -> dict:
    res = _residues([a for a in atoms if a.record == "ATOM" or is_het_polymer(a)])
    miss_atoms = {}
    for (chain, seq, ic, name), ats in res.items():
        exp = EXPECTED_HEAVY.get(name if name in EXPECTED_HEAVY else {"HID": "HIS", "HIE": "HIS", "HIP": "HIS"}.get(name, ""))
        if exp is None:
            continue
        have = {a.name for a in ats}
        gone = sorted(exp - have)
        if gone:
            miss_atoms[rid(chain, seq, ic) + " " + name] = gone
    miss_res = [(c, n, s) for c, n, s in st.missing_residues if c in chains]
    ranges: list[dict] = []
    for c in sorted({m[0] for m in miss_res}):
        nums = sorted(s for cc, _, s in miss_res if cc == c)
        start = prev = nums[0]
        for s in nums[1:] + [None]:  # type: ignore[list-item]
            if s is None or s != prev + 1:
                ranges.append({"chain": c, "first": start, "last": prev, "n": prev - start + 1})
                if s is not None:
                    start = s
            if s is not None:
                prev = s
    # chain breaks from CA-CA distance (independent of REMARK 465)
    breaks = []
    cas = [a for a in atoms if a.name == "CA" and (a.record == "ATOM")]
    for a, b in zip(cas, cas[1:]):
        if a.chain == b.chain and ((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2) ** 0.5 > 4.2:
            breaks.append({"chain": a.chain, "after": a.resseq, "before": b.resseq})
    if ranges:
        log.add("missing", "missing residues reported (not modelled)", "", f"{sum(r['n'] for r in ranges)} residue(s) unobserved in {len(ranges)} segment(s)", segments=ranges, source="REMARK 465 / unobserved-residue table")
    if breaks:
        log.add("missing", "chain breaks detected from CA-CA distance > 4.2 A", "", f"{len(breaks)} break(s); not repaired", breaks=breaks)
    if miss_atoms:
        log.add("missing", "residues with missing heavy atoms", "", f"{len(miss_atoms)} residue(s)", residues=miss_atoms,
                action_taken="atoms rebuilt by PDB2PQR if run (see hydrogen step), otherwise left missing")
    return {"missing_residue_segments": ranges, "chain_breaks": breaks, "residues_missing_atoms": miss_atoms}


def _run_pdb2pqr(atoms: list[Atom], ph: float, log: ChangeLog) -> list[Atom] | None:
    exe = find_pdb2pqr()
    if exe is None:
        return None
    with tempfile.TemporaryDirectory(prefix="mdp_") as td:
        inp, outpdb, outpqr = Path(td, "in.pdb"), Path(td, "out.pdb"), Path(td, "out.pqr")
        inp.write_text(write_pdb(atoms))
        argv = [exe, "--ff", "AMBER", "--with-ph", f"{ph:.2f}", "--titration-state-method", "propka",
                "--pdb-output", str(outpdb), "--keep-chain", "--drop-water", str(inp), str(outpqr)]
        try:
            r = subprocess.run(argv, cwd=td, capture_output=True, timeout=SUBPROCESS_TIMEOUT_S, check=False,  # nosec B603
                               env={"PATH": os.environ.get("PATH", ""), "HOME": td})
        except subprocess.TimeoutExpired:
            log.add("hydrogens", "PDB2PQR timed out", "", "falling back to no-hydrogen method")
            return None
        if r.returncode != 0 or not outpdb.exists():
            log.add("hydrogens", "PDB2PQR failed", "", f"exit code {r.returncode}; falling back to no-hydrogen method")
            return None
        return parse_pdb(outpdb.read_text()).atoms


def _infer_states(atoms: list[Atom]) -> list[Atom]:
    """PDB2PQR keeps HIS/ASP/GLU/LYS/CYS names in PDB output; infer the state from which hydrogens exist and rename."""
    for (c, s, ic, name), ats in _residues(atoms).items():
        names = {a.name for a in ats}
        new = name
        if name == "HIS":
            d, e = "HD1" in names, "HE2" in names
            new = "HIP" if d and e else "HID" if d else "HIE" if e else "HIS"
        elif name == "ASP" and "HD2" in names:
            new = "ASH"
        elif name == "GLU" and "HE2" in names:
            new = "GLH"
        elif name == "LYS" and "HZ3" not in names and "HZ2" in names:
            new = "LYN"
        elif name == "CYS" and "HG" not in names and "SG" in names and any(a.is_h for a in ats):
            new = "CYX_OR_CYM"
        if new != name and new != "CYX_OR_CYM":
            for a in ats:
                a.resname = new
    return atoms


def add_hydrogens(prot: list[Atom], ph: float, log: ChangeLog) -> tuple[list[Atom], dict]:
    new = _run_pdb2pqr(prot, ph, log)
    if new is None:
        counts = Counter(a.resname for a in {(a.chain, a.resseq, a.icode): a for a in prot}.values() if a.resname in TITRATABLE_PKA)
        states = {}
        for (c, s, ic), a in {(a.chain, a.resseq, a.icode): a for a in prot}.items():
            if a.resname in TITRATABLE_PKA:
                pka = TITRATABLE_PKA[a.resname]
                acid = a.resname in ("ASP", "GLU", "CYS", "TYR")
                states[rid(c, s, ic) + " " + a.resname] = ("deprotonated expected" if ph > pka else "protonated expected") if acid else ("protonated expected" if ph < pka else "neutral expected")
        log.add("hydrogens", "no hydrogens added (PDB2PQR not available)", "", "documented fallback: expected charge state of titratable residues from model pKa only; no hydrogens or heavy atoms were added",
                ph=ph, method="model-pKa table, no structure-based shifts", counts=dict(counts), states=states)
        return prot, {"method": "none (model-pKa table fallback)", "hydrogens_added": 0, "pdb2pqr": False}
    new = _infer_states(new)
    # compare
    old = {(a.chain, a.resseq, a.icode): a.resname for a in prot}
    n_old_heavy = {(a.chain, a.resseq, a.icode, a.name) for a in prot if not a.is_h}
    changes = []
    for a in new:
        k = (a.chain, a.resseq, a.icode)
        if k in old and old[k] != a.resname:
            changes.append((k, old[k], a.resname))
            old[k] = a.resname
    seen = set()
    for k, was, now in changes:
        if k in seen:
            continue
        seen.add(k)
        log.add("protonation", "residue protonation state set", rid(*k), f"{was} -> {now} at pH {ph}", method="PROPKA 3 via PDB2PQR", ph=ph)
    added_heavy = [a for a in new if not a.is_h and (a.chain, a.resseq, a.icode, a.name) not in n_old_heavy]
    for a in added_heavy:
        log.add("missing", "heavy atom rebuilt", f"{rid(a.chain, a.resseq, a.icode)} {a.resname} {a.name}", "added by PDB2PQR")
    nh = sum(1 for a in new if a.is_h)
    log.add("hydrogens", "hydrogens added", "", f"{nh} hydrogen atom(s) added", ph=ph, method="PDB2PQR (AMBER names) with PROPKA titration",
            heavy_atoms_rebuilt=len(added_heavy))
    return new, {"method": "PDB2PQR + PROPKA", "hydrogens_added": nh, "pdb2pqr": True}


def _ad_type_protein(a: Atom, resatoms: list[Atom]) -> str | None:
    el = a.element.upper()
    if a.is_h:
        heavy = [b for b in resatoms if not b.is_h]
        if not heavy:
            return None
        nb = min(heavy, key=lambda b: (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)
        d = ((a.x - nb.x) ** 2 + (a.y - nb.y) ** 2 + (a.z - nb.z) ** 2) ** 0.5
        return "HD" if nb.element.upper() in ("N", "O", "S") and d < 1.35 else None
    rn = {"HID": "HIS", "HIE": "HIS", "HIP": "HIS"}.get(a.resname, a.resname)
    if el == "C":
        return "A" if a.name in _AROM.get(rn, ()) else "C"
    if el == "N":
        if rn == "HIS" and a.name in ("ND1", "NE2"):
            has_h = any(b.is_h and b.name.startswith("H" + a.name[1:]) for b in resatoms)
            if a.resname == "HIS":
                return "NA"
            return "N" if has_h or a.resname == "HIP" else "NA"
        return "N"
    if el == "O":
        return "OA"
    if el == "S":
        return "SA"
    return {"ZN": "Zn", "MG": "Mg", "MN": "Mn", "FE": "Fe", "CA": "Ca", "CU": "Cu", "CO": "Co", "NI": "Ni", "CD": "Cd",
            "HG": "Hg", "F": "F", "CL": "Cl", "BR": "Br", "I": "I", "P": "P", "SE": "S"}.get(el, el.capitalize()[:2])


def write_receptor_pdbqt(atoms: list[Atom], log: ChangeLog) -> str:
    lines = ["REMARK  Receptor prepared by Metis dockprep; rigid; charges set to 0.000 (Vina/GNINA ignore receptor charges); nonpolar H dropped"]
    res = _residues(atoms)
    serial = 0
    dropped = 0
    for (c, s, ic, name), ats in res.items():
        for a in ats:
            t = _ad_type_protein(a, ats)
            if t is None:
                dropped += 1
                continue
            serial += 1
            lines.append(pdbqt_atom_line(serial, a.name[:4], name, c, s, (a.x, a.y, a.z), 0.0, t, a.record, a.occ, a.bfac, ic))
    lines.append("TER")
    log.add("pdbqt", "write receptor PDBQT", "", f"{serial} atoms written; {dropped} nonpolar hydrogens dropped",
            charges="0.000 for all atoms", atom_types="simplified AutoDock typing")
    return "\n".join(lines) + "\n"


def prepare_protein(data: bytes, chains: list[str] | None = None, remove_waters: bool = True, remove_heteroatoms: bool = True,
                    keep_hets: list[str] | None = None, keep_metals: bool = True, add_h: bool = True, ph: float = 7.4,
                    pdb_id: str | None = None) -> dict:
    if not (0.0 <= ph <= 14.0):
        raise DockprepError("bad_param", "pH must be between 0 and 14.", 422)
    keep_hets = [k.upper() for k in (keep_hets or [])]
    st = parse_structure(data)
    log = ChangeLog()
    log.add("input", "parsed structure", "", f"{len(st.atoms)} atoms read (first model only)", format=st.source_format,
            experimental_method=st.expdta or "not stated in file", method_class=st.method_class, resolution_angstrom=st.resolution,
            resolution_source=st.resolution_source or None, source=pdb_id or "uploaded file")
    if st.n_models_seen > 1:
        log.add("input", "multiple models present", "", "only the first model was used")
    all_chains = sorted({a.chain for a in st.atoms if a.record == "ATOM"})
    sel = set(chains) if chains else set(all_chains)
    bad = sel - {a.chain for a in st.atoms}
    if bad:
        raise DockprepError("bad_param", "Requested chain not found in structure.", 422)
    atoms = list(st.atoms)
    # chain selection
    dropped_chain_atoms = Counter(a.chain for a in atoms if a.chain not in sel)
    for c, n in sorted(dropped_chain_atoms.items()):
        log.add("chains", "chain removed", f"chain {c}", f"{n} atoms dropped (not selected)")
    atoms = [a for a in atoms if a.chain in sel]
    log.add("chains", "chains kept", ",".join(sorted(sel)), f"available chains: {','.join(all_chains)}")
    atoms = resolve_altlocs(atoms, log)
    # waters
    waters = _residues([a for a in atoms if a.resname in WATER])
    if remove_waters:
        for (c, s, ic, n) in waters:
            log.add("waters", "water removed", rid(c, s, ic), f"{n}")
        atoms = [a for a in atoms if a.resname not in WATER]
        log.add("waters", "summary", "", f"{len(waters)} water(s) removed")
    else:
        log.add("waters", "waters kept", "", f"{len(waters)} water(s) kept")
    # hetero groups
    hets = classify_hets(atoms)
    cocrystal = []
    keep_ids = set()
    for h in hets:
        ident = rid(h["chain"], h["resseq"], h["icode"])
        kept = False
        if h["resname"] in keep_hets or ident in keep_hets:
            kept, why = True, "listed in keep_hets"
        elif h["kind"] == "metal_ion" and keep_metals:
            kept, why = True, "metal ion kept (keep_metals)"
        elif not remove_heteroatoms:
            kept, why = True, "remove_heteroatoms is off"
        else:
            why = "heteroatom removal on"
        if h["kind"] == "cocrystal_ligand_candidate":
            cocrystal.append(h)
            log.add("hetero", "co-crystal ligand identified", f"{ident} {h['resname']}", f"{h['n_heavy']} heavy atoms; written to a separate reference file")
            if kept and (h["resname"] in keep_hets or ident in keep_hets):
                pass
            else:
                kept = False
                why = "co-crystal ligand is not part of the receptor; saved as reference file"
        log.add("hetero", "kept in receptor" if kept else "removed from receptor", f"{ident} {h['resname']}",
                f"{h['kind']}, {h['n_heavy']} heavy atoms; {why}")
        if kept:
            keep_ids.add((h["chain"], h["resseq"], h["icode"], h["resname"]))
    atoms = [a for a in atoms if not (a.record == "HETATM" and a.resname not in WATER and not is_het_polymer(a) and (a.chain, a.resseq, a.icode, a.resname) not in keep_ids)]
    prot = [a for a in atoms if a.record == "ATOM" or is_het_polymer(a)]
    prot_ids = {id(a) for a in prot}                      # identity set: O(n), not O(n^2) dataclass-equality list scans
    het_kept = [a for a in atoms if id(a) not in prot_ids]
    if not prot:
        raise DockprepError("bad_structure", "No protein atoms remain after preparation.", 422)
    missing = detect_missing(prot, st, sel, log)
    hinfo = {"method": "not requested", "hydrogens_added": 0, "pdb2pqr": False}
    if add_h:
        prot, hinfo = add_hydrogens(prot, ph, log)
        if het_kept:
            log.add("hydrogens", "kept hetero groups not protonated", "", f"{len(het_kept)} atoms left as in the input")
    else:
        log.add("hydrogens", "hydrogens not requested", "")
    final = prot + het_kept
    receptor_pdb = write_pdb(final, [f"Prepared by Metis dockprep; pH {ph}; see change log"])
    receptor_pdbqt = write_receptor_pdbqt(final, log)
    files = {"receptor.pdb": receptor_pdb, "receptor.pdbqt": receptor_pdbqt}
    refs = []
    for h in cocrystal:
        fn = "cocrystal_" + sanitize_name(f"{h['resname']}_{h['chain']}{h['resseq']}") + ".pdb"
        files[fn] = write_pdb(h["atoms"], [f"Co-crystal ligand {h['resname']} from the input structure (experimental coordinates)"])
        refs.append({"resname": h["resname"], "residue": rid(h["chain"], h["resseq"], h["icode"]), "n_heavy_atoms": h["n_heavy"], "file": fn,
                     "name_in_entry": st.het_names.get(h["resname"], "")})
    report = {
        "pdb_id": st.pdb_id or (pdb_id or ""), "experimental_method": st.expdta or None, "method_class": st.method_class,
        "resolution_angstrom": st.resolution, "resolution_source": st.resolution_source or None,
        "method_note": ("Cryo-EM: resolution varies across the map; side-chain and ligand positions at the pocket may be less certain than the global value."
                        if st.method_class == "cryoem" else ""),
        "chains_available": all_chains, "chains_selected": sorted(sel), "n_waters_removed": len(waters) if remove_waters else 0,
        "cocrystal_ligands": refs, "hydrogens": hinfo, "ph": ph,
        "n_residues": len(_residues(prot)), **missing,
    }
    return {"files": files, "change_log": log.entries, "report": report}
