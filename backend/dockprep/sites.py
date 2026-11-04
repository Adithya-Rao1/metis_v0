# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310
"""Binding-site definition: boxes, detected pockets, literature-linked (PDB-entry annotations only) and
uncertainty facts per site. Facts, not verdicts: every statement gives the observed value, the threshold used and
the residues involved."""
from __future__ import annotations

import math
import statistics

import numpy as np

from . import pockets as pk
from .errors import DockprepError
from .pdbio import WATER, Atom, Structure, parse_structure
from .protein import classify_hets, rid

DEFAULT_PADDING = 4.0
MIN_EDGE = 10.0
RES_LOW_THRESHOLD = 3.0
BFACTOR_RATIO = 1.5
FLEX_TYPES = frozenset({"LYS", "ARG", "GLU", "GLN", "MET"})
AGREE_DIST = 6.0


def _box(points: np.ndarray, padding: float) -> tuple[list[float], list[float]]:
    lo, hi = points.min(axis=0), points.max(axis=0)
    size = np.maximum(hi - lo + 2 * padding, MIN_EDGE)
    return ((lo + hi) / 2).round(3).tolist(), size.round(3).tolist()


def _parse_res_id(s: str) -> tuple[str, int, str]:
    try:
        chain, num = s.strip().split(":")
        icode = num[-1] if num[-1].isalpha() else ""
        return chain, int(num[:-1] if icode else num), icode
    except (ValueError, IndexError):
        raise DockprepError("bad_param", "Residue ids must look like A:790.", 422) from None


def _check_padding(padding: float) -> None:
    if not math.isfinite(padding) or not (0 <= padding <= 20):
        raise DockprepError("bad_param", "Padding must be between 0 and 20 A.", 422)


def site_from_residues(atoms: list[Atom], ids: list[str], padding: float = DEFAULT_PADDING) -> dict:
    _check_padding(padding)
    want = {_parse_res_id(i) for i in ids}
    sel = [a for a in atoms if a.rkey in want and not a.is_h]
    found = {a.rkey for a in sel}
    if not sel or (want - found):
        raise DockprepError("bad_param", "One or more requested residues were not found.", 422)
    pts = np.array([[a.x, a.y, a.z] for a in sel])
    c, s = _box(pts, padding)
    return {"source": "user_residues", "method": "user-picked residues", "label": f"residues {', '.join(sorted(ids))}",
            "center": c, "size": s, "padding_A": padding, "residues": sorted({f"{rid(*k)}" for k in found}), "score": None, "citation": None}


def site_from_ligand(atoms: list[Atom], ligand_id: str | None, padding: float = DEFAULT_PADDING) -> dict:
    _check_padding(padding)
    hets = [h for h in classify_hets(atoms) if h["kind"] == "cocrystal_ligand_candidate"]
    if ligand_id:
        want = _parse_res_id(ligand_id.split()[0])
        hets = [h for h in hets if (h["chain"], h["resseq"], h["icode"]) == want]
    if not hets:
        raise DockprepError("bad_param", "No co-crystal ligand found.", 422)
    h = hets[0]
    pts = np.array([[a.x, a.y, a.z] for a in h["atoms"] if not a.is_h])
    c, s = _box(pts, padding)
    prot = [a for a in atoms if a.record == "ATOM"]
    lining = pk.lining_residues(prot, pts, 4.5)
    return {"source": "cocrystal_ligand", "method": "co-crystal ligand", "label": f"ligand {h['resname']} {rid(h['chain'], h['resseq'], h['icode'])}",
            "center": c, "size": s, "padding_A": padding, "residues": lining, "ligand": {"resname": h["resname"], "n_heavy_atoms": h["n_heavy"]},
            "score": None, "citation": None}


def site_from_box(center: list[float], size: list[float]) -> dict:
    from .configs import _check_box

    _check_box(center, size)
    return {"source": "manual_box", "method": "manual box", "label": "manual box", "center": [round(float(x), 3) for x in center],
            "size": [round(float(x), 3) for x in size], "residues": [], "score": None, "citation": None}


def detected_sites(atoms: list[Atom], padding: float = DEFAULT_PADDING, max_pockets: int = 5) -> tuple[list[dict], dict]:
    _check_padding(padding)
    prot = [a for a in atoms if a.record == "ATOM"]
    sites: list[dict] = []
    info = {"fpocket": "unavailable", "geometric": "run"}
    fp = pk.run_fpocket(prot)
    if fp is not None:
        info["fpocket"] = "run"
        for i, p in enumerate(fp[:max_pockets], 1):
            c, s = _box(p["points"], padding)
            sc = p.get("score")
            sites.append({"source": "detected", "method": "fpocket", "label": f"fpocket pocket {i}", "center": c, "size": s, "padding_A": padding,
                          "residues": pk.lining_residues(prot, p["points"]),
                          "score": {"value": sc, "name": "fpocket score", "method_note": "fpocket pocket score (CLI output); druggability fields in raw info are not asserted here"},
                          "citation": None})
    for i, p in enumerate(pk.geometric_pockets(prot, max_pockets=max_pockets), 1):
        c, s = _box(p["points"], padding)
        sites.append({"source": "detected", "method": pk.GEOMETRIC_METHOD, "label": f"geometric pocket {i} (fallback finder)", "center": c, "size": s,
                      "padding_A": padding, "residues": pk.lining_residues(prot, p["points"]),
                      "score": {"value": round(p["score"], 2), "name": "volume x mean buriedness", "method_note": pk.GEOMETRIC_NOTE,
                                "volume_A3": p["volume_A3"], "mean_buriedness": round(p["mean_buriedness"], 3)},
                      "citation": None})
    return sites, info


def literature_sites(st: Structure, padding: float = DEFAULT_PADDING) -> list[dict]:
    """Sites asserted only from annotations inside the structure file itself. Never generated by a language model."""
    if not st.pdb_id:
        src = "uploaded structure file (no PDB ID in header)"
    else:
        src = f"PDB entry {st.pdb_id}"
    by_res: dict[tuple, list[Atom]] = {}
    for a in st.atoms:
        by_res.setdefault((a.chain, a.resseq), []).append(a)
    out = []
    for s in st.sites:
        pts = [(a.x, a.y, a.z) for key in s["residues"] for a in by_res.get((key[1], key[2]), []) if not a.is_h]
        if not pts:
            continue
        c, sz = _box(np.array(pts), padding)
        out.append({"source": "literature", "method": "deposited annotation", "label": f"{src} SITE {s['id']}", "center": c, "size": sz, "padding_A": padding,
                    "residues": [f"{ch}:{n} {rn}" for rn, ch, n in s["residues"]], "score": None,
                    "citation": {"source": src, "records": "SITE / REMARK 800", "site_id": s["id"], "description": s["description"] or None,
                                 "evidence_code": s.get("evidence_code") or None,
                                 "note": "Annotation written into the structure entry (by depositors or software). It is not an independent literature review."}})
    het_sites = [h for h in classify_hets(st.atoms) if h["kind"] == "cocrystal_ligand_candidate"]
    for h in het_sites:
        pts = np.array([[a.x, a.y, a.z] for a in h["atoms"] if not a.is_h])
        c, sz = _box(pts, padding)
        links = [lk for lk in st.links if h["resname"] in (lk["a"][0], lk["b"][0])]
        out.append({"source": "literature", "method": "deposited annotation", "label": f"{src} ligand {h['resname']} {rid(h['chain'], h['resseq'], h['icode'])}",
                    "center": c, "size": sz, "padding_A": padding, "residues": [], "score": None,
                    "citation": {"source": src, "records": "HETATM" + (" / HETNAM" if h["resname"] in st.het_names else "") + (" / LINK" if links else ""),
                                 "ligand_name_in_entry": st.het_names.get(h["resname"]) or None, "n_link_records": len(links),
                                 "note": "Ligand modelled in the deposited entry; the entry is the source, not an independent review."}})
    return out


def merge_agreement(sites: list[dict]) -> list[dict]:
    methods = sorted({s["method"] for s in sites})
    for s in sites:
        agree, dist = [], {}
        for o in sites:
            if o is s or o["method"] == s["method"]:
                continue
            d = float(np.linalg.norm(np.array(s["center"]) - np.array(o["center"])))
            if d <= AGREE_DIST or _overlap(s, o):
                agree.append(o["method"])
                dist[o["method"]] = round(d, 2)
        s["agreement"] = {"methods_agreeing": sorted(set(agree)), "n_agreeing": len(set(agree)), "methods_run": methods,
                          "center_distance_A": dist, "rule": f"another method has a site with centre within {AGREE_DIST} A or the search boxes overlap"}
    return sites


def _overlap(a: dict, b: dict) -> bool:
    ca, sa, cb, sb = (np.array(x) for x in (a["center"], a["size"], b["center"], b["size"]))
    inter = np.minimum(ca + sa / 2, cb + sb / 2) - np.maximum(ca - sa / 2, cb - sb / 2)
    if (inter <= 0).any():
        return False
    v = float(np.prod(inter))
    return v / min(float(np.prod(sa)), float(np.prod(sb))) >= 0.5


def uncertainty_facts(st: Structure, site: dict, atoms_raw: list[Atom]) -> list[dict]:
    """Facts about how well this site is determined in this structure. No verdicts."""
    facts: list[dict] = []
    res_ids = {r.split(" ")[0] for r in site.get("residues", [])}
    facts.append({"kind": "method_and_resolution", "applies": st.resolution is not None and st.resolution > RES_LOW_THRESHOLD,
                  "observed": {"method": st.expdta or "not stated", "resolution_A": st.resolution, "source": st.resolution_source or None},
                  "threshold_used": f"{RES_LOW_THRESHOLD} A (entries reported above this are listed as lower resolution)",
                  "explanation": ("Reported resolution is above the threshold: side-chain, water and ligand positions are less precisely determined." if st.resolution is not None and st.resolution > RES_LOW_THRESHOLD
                                  else "Resolution is at or below the threshold." if st.resolution is not None else "No resolution is stated in the file."),
                  "residues": []})
    if st.method_class == "cryoem":
        facts.append({"kind": "cryoem_local_resolution", "applies": True, "observed": {"method": st.expdta},
                      "explanation": "Cryo-EM maps have resolution that varies across the structure; the value in the header is global and local resolution at this site is not in the file.",
                      "residues": []})
    # B-factors
    per_res: dict[tuple, list[float]] = {}
    for a in atoms_raw:
        if a.record == "ATOM" and not a.is_h:
            per_res.setdefault(a.rkey, []).append(a.bfac)
    means = {k: statistics.fmean(v) for k, v in per_res.items()}
    if means:
        med = statistics.median(means.values())
        hi = [(k, round(means[k], 1)) for k in sorted(means) if f"{rid(*k)}" in res_ids and means[k] > BFACTOR_RATIO * med and med > 0]
        facts.append({"kind": "high_b_factors", "applies": bool(hi),
                      "observed": {"protein_median_residue_B": round(med, 1), "residues_above": [{"residue": rid(*k), "mean_B": b} for k, b in hi]},
                      "threshold_used": f"{BFACTOR_RATIO} x protein median of per-residue mean B",
                      "explanation": "Lining residues with mean B-factor above the threshold are less well ordered in the model." if hi else "No lining residue exceeds the threshold.",
                      "residues": [rid(*k) for k, _ in hi]})
    # missing residues near site
    center = np.array(site["center"])
    near_missing = []
    by_chain: dict[tuple, list[Atom]] = {}
    for a in atoms_raw:
        by_chain.setdefault((a.chain, a.resseq), []).append(a)
    for (c, resn, seq) in st.missing_residues:
        for flank in (seq - 1, seq + 1):
            ats = by_chain.get((c, flank))
            if ats:
                d = min(math.dist(center, (a.x, a.y, a.z)) for a in ats)
                if d <= 15.0:
                    near_missing.append({"missing": f"{c}:{seq}", "neighbour_resolved": f"{c}:{flank}", "distance_to_site_centre_A": round(d, 1)})
    facts.append({"kind": "missing_residues_near_site", "applies": bool(near_missing),
                  "observed": {"entries": near_missing[:30]}, "threshold_used": "resolved neighbour of an unobserved residue within 15 A of the site centre (REMARK 465)",
                  "explanation": "Unobserved residues adjoin resolved residues near this site; the loop conformation is not in the structure." if near_missing else "No unobserved residue adjoins the site within the distance used.",
                  "residues": sorted({n["missing"] for n in near_missing})})
    # flexibility
    alt = sorted({rid(*a.rkey) for a in atoms_raw if a.altloc and rid(*a.rkey) in res_ids})
    partial = sorted({rid(*a.rkey) for a in atoms_raw if a.record == "ATOM" and a.occ < 1.0 and not a.altloc and rid(*a.rkey) in res_ids})
    long_sc = [r for r in site.get("residues", []) if r.split(" ")[-1] in FLEX_TYPES]
    facts.append({"kind": "flexible_residues", "applies": bool(alt or partial),
                  "observed": {"alternate_conformations_in_file": alt, "occupancy_below_1": partial, "long_flexible_side_chains_listed_for_reference": long_sc},
                  "explanation": "Alternate conformations or partial occupancy in the file are direct evidence of more than one side-chain position. Long side-chain residue types are listed for reference only; the file holds no evidence they move.",
                  "residues": alt + partial})
    return facts


def analyze_sites(data: bytes, modes: list[str], residues: list[str] | None = None, ligand_id: str | None = None,
                  box: dict | None = None, padding: float = DEFAULT_PADDING, chains: list[str] | None = None) -> dict:
    st = parse_structure(data)
    atoms = [a for a in st.atoms if (not chains or a.chain in chains) and a.resname not in WATER]
    sites: list[dict] = []
    notes: list[str] = []
    info: dict = {}
    if "residues" in modes:
        sites.append(site_from_residues(atoms, residues or [], padding))
    if "ligand" in modes:
        sites.append(site_from_ligand(atoms, ligand_id, padding))
    if "box" in modes:
        if not box:
            raise DockprepError("bad_param", "A box needs center and size.", 422)
        sites.append(site_from_box(box.get("center", []), box.get("size", [])))
    if "detect" in modes:
        d, info = detected_sites(atoms, padding)
        sites.extend(d)
        if info.get("fpocket") == "unavailable":
            notes.append("fpocket binary not available: only the labelled geometric fallback finder was run.")
    if "literature" in modes:
        lit = literature_sites(st, padding)
        sites.extend(lit)
        if not lit:
            notes.append("No SITE, REMARK 800 or ligand annotation found in this file; no literature-linked site is asserted.")
    merge_agreement(sites)
    for i, s in enumerate(sites, 1):
        s["id"] = f"site{i}"
        s["uncertainty"] = uncertainty_facts(st, s, atoms)
        s["warnings"] = [f"search box edge {e:.1f} A exceeds 30 A; Vina accuracy and runtime degrade with box volume at fixed exhaustiveness"
                         for e in [max(s["size"])] if e > 30.0]
    return {"sites": sites, "notes": notes, "detection": info,
            "structure": {"pdb_id": st.pdb_id, "method": st.expdta or None, "method_class": st.method_class, "resolution_angstrom": st.resolution}}
