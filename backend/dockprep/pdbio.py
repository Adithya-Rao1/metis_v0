"""Pure-python PDB / mmCIF reader and PDB writer. Content is treated purely as data (never executed).

Only the first model is read. All numeric fields are validated; errors carry fixed messages.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Iterable
from dataclasses import dataclass, field

from .errors import DockprepError
from .limits import MAX_ATOMS, MAX_LINE_LEN

WATER = frozenset({"HOH", "WAT", "H2O", "DOD", "TIP", "TIP3"})
STD_AA = frozenset(
    [
        "ALA",
        "ARG",
        "ASN",
        "ASP",
        "CYS",
        "GLN",
        "GLU",
        "GLY",
        "HIS",
        "ILE",
        "LEU",
        "LYS",
        "MET",
        "PHE",
        "PRO",
        "SER",
        "THR",
        "TRP",
        "TYR",
        "VAL",
        "HID",
        "HIE",
        "HIP",
        "ASH",
        "GLH",
        "CYX",
        "CYM",
        "LYN",
    ]
)


@dataclass(slots=True)
class Atom:
    record: str  # ATOM | HETATM
    serial: int
    name: str
    altloc: str
    resname: str
    chain: str
    resseq: int
    icode: str
    x: float
    y: float
    z: float
    occ: float
    bfac: float
    element: str

    @property
    def rkey(self) -> tuple[str, int, str]:
        return (self.chain, self.resseq, self.icode)

    @property
    def is_h(self) -> bool:
        return self.element in ("H", "D")


@dataclass
class Structure:
    atoms: list[Atom] = field(default_factory=list)
    pdb_id: str = ""
    expdta: str = ""
    method_class: str = "unknown"  # xray | cryoem | nmr | other | unknown
    resolution: float | None = None
    resolution_source: str = ""
    missing_residues: list[tuple[str, str, int]] = field(default_factory=list)  # chain, resname, resseq
    het_names: dict[str, str] = field(default_factory=dict)
    sites: list[dict] = field(default_factory=list)  # id, residues[(resname,chain,resseq)], description
    links: list[dict] = field(default_factory=list)
    remark800: list[str] = field(default_factory=list)
    n_models_seen: int = 1
    source_format: str = "pdb"


def classify_method(expdta: str) -> str:
    u = expdta.upper()
    if "ELECTRON MICROSCOPY" in u or "ELECTRON CRYSTALLOGRAPHY" in u or "CRYO" in u:
        return "cryoem"
    if "X-RAY" in u or "NEUTRON" in u:
        return "xray"
    if "NMR" in u:
        return "nmr"
    return "other" if u.strip() else "unknown"


def _f(s: str) -> float:
    v = float(s)
    if not math.isfinite(v) or abs(v) > 1e5:
        raise ValueError
    return v


def _element_from(name: str, el_field: str, resname: str, record: str) -> str:
    el = el_field.strip().capitalize()
    if el:
        return "H" if el == "D" else el
    n = name.strip()
    if record == "ATOM" or resname in STD_AA:
        return (n.lstrip("0123456789")[:1] or "C").upper()
    return n.lstrip("0123456789")[:1].upper() or "C"


def parse_pdb(text: str) -> Structure:
    st = Structure()
    in_model1 = True
    seen_model = False
    for raw in text.splitlines():
        if len(raw) > MAX_LINE_LEN:
            raise DockprepError("bad_structure", "Structure file has an over-long line.", 422)
        rec = raw[:6].strip()
        if rec == "MODEL":
            if seen_model:
                in_model1 = False
            seen_model = True
            continue
        if rec == "ENDMDL":
            if seen_model:
                in_model1 = False
                st.n_models_seen += 1
            continue
        if rec == "HEADER":
            st.pdb_id = raw[62:66].strip().upper()
        elif rec == "EXPDTA":
            st.expdta = (st.expdta + " " + raw[10:].strip()).strip()
            st.method_class = classify_method(st.expdta)
        elif rec == "REMARK":
            _remark(st, raw)
        elif rec == "HETNAM":
            code = raw[11:14].strip()
            if code:
                st.het_names[code] = (st.het_names.get(code, "") + " " + raw[15:70].strip()).strip()
        elif rec == "SITE":
            _site(st, raw)
        elif rec == "LINK":
            st.links.append(
                {"a": _res_from(raw, 12, 17, 21, 22, 26), "b": _res_from(raw, 42, 47, 51, 52, 56)}
            )
        elif rec in ("ATOM", "HETATM") and in_model1:
            try:
                a = Atom(
                    record=rec,
                    serial=int(raw[6:11] or 0),
                    name=raw[12:16].strip(),
                    altloc=raw[16:17].strip(),
                    resname=raw[17:20].strip(),
                    chain=raw[21:22].strip() or "_",
                    resseq=int(raw[22:26]),
                    icode=raw[26:27].strip(),
                    x=_f(raw[30:38]),
                    y=_f(raw[38:46]),
                    z=_f(raw[46:54]),
                    occ=_f(raw[54:60]) if raw[54:60].strip() else 1.0,
                    bfac=_f(raw[60:66]) if raw[60:66].strip() else 0.0,
                    element=_element_from(raw[12:16], raw[76:78], raw[17:20].strip(), rec),
                )
            except ValueError:
                raise DockprepError(
                    "bad_structure", "Structure file has a malformed atom record.", 422
                ) from None
            st.atoms.append(a)
            if len(st.atoms) > MAX_ATOMS:
                raise DockprepError("too_large", "Structure has too many atoms.", 413)
    _finish(st)
    return st


def _res_from(raw: str, n0: int, n1: int, c: int, s0: int, s1: int) -> tuple[str, str, int]:
    try:
        return (raw[n0:n1].strip(), raw[c : c + 1].strip() or "_", int(raw[s0:s1]))
    except ValueError:
        return ("", "_", 0)


def _site(st: Structure, raw: str) -> None:
    sid = raw[11:14].strip()
    if not sid:
        return
    site = next((s for s in st.sites if s["id"] == sid), None)
    if site is None:
        site = {"id": sid, "residues": [], "description": ""}
        st.sites.append(site)
    for i in range(4):
        s = 18 + 11 * i
        if len(raw) < s + 9 or not raw[s : s + 3].strip():
            continue
        try:
            site["residues"].append(
                (raw[s : s + 3].strip(), raw[s + 4 : s + 5].strip() or "_", int(raw[s + 5 : s + 9]))
            )
        except ValueError:
            continue


def _remark(st: Structure, raw: str) -> None:
    try:
        num = int(raw[7:10])
    except ValueError:
        return
    body = raw[11:].strip()
    if num == 2 and "RESOLUTION." in body and "ANGSTROM" in body.upper():
        tok = body.split("RESOLUTION.")[1].split()[0]
        with contextlib.suppress(ValueError):
            st.resolution, st.resolution_source = float(tok), "REMARK 2"
    elif (
        num == 3
        and st.resolution is None
        and "RESOLUTION" in body
        and ":" in body
        and "RANGE LOW" not in body
    ):
        # X-ray refinement "RESOLUTION RANGE HIGH (ANGSTROMS) : 2.60"
        if "HIGH" in body or body.startswith("RESOLUTION"):
            with contextlib.suppress(ValueError, IndexError):
                st.resolution, st.resolution_source = float(body.split(":")[1].split()[0]), "REMARK 3"
    elif num == 465:
        parts = body.split()
        if len(parts) == 3 and parts[0].isalpha() and parts[2].lstrip("-").isdigit() and len(parts[0]) <= 4:
            st.missing_residues.append((parts[1], parts[0], int(parts[2])))
        elif len(parts) == 4 and parts[0].isdigit() and parts[3].lstrip("-").isdigit():  # model-numbered form
            st.missing_residues.append((parts[2], parts[1], int(parts[3])))
    elif num == 800:
        if body:
            st.remark800.append(body)


def _finish(st: Structure) -> None:
    # attach REMARK 800 text to sites
    cur = None
    desc: dict[str, list[str]] = {}
    evid: dict[str, str] = {}
    for line in st.remark800:
        if line.startswith("SITE_IDENTIFIER:"):
            cur = line.split(":", 1)[1].strip()
            desc[cur] = []
        elif line.startswith("EVIDENCE_CODE:") and cur:
            evid[cur] = line.split(":", 1)[1].strip()
        elif line.startswith("SITE_DESCRIPTION:") and cur:
            desc[cur].append(line.split(":", 1)[1].strip())
        elif cur and not line.startswith(("SITE", "EVIDENCE", "REMARK")):
            desc[cur].append(line.strip())
    for s in st.sites:
        s["description"] = " ".join(desc.get(s["id"], []))[:500]
        s["evidence_code"] = evid.get(s["id"], "")


# ---------------- mmCIF (minimal _atom_site reader) ----------------
def _cif_tokens(line: str) -> list[str]:
    out, i, n = [], 0, len(line)
    while i < n:
        c = line[i]
        if c.isspace():
            i += 1
        elif c in "'\"":
            j = i + 1
            while j < n and not (line[j] == c and (j + 1 == n or line[j + 1].isspace())):
                j += 1
            out.append(line[i + 1 : j])
            i = j + 1
        elif c == "#":
            break
        else:
            j = i
            while j < n and not line[j].isspace():
                j += 1
            out.append(line[i:j])
            i = j
    return out


def parse_mmcif(text: str) -> Structure:
    st = Structure(source_format="mmcif")
    lines = text.splitlines()
    i = 0
    n = len(lines)
    scalars: dict[str, str] = {}
    loops: dict[str, tuple[list[str], list[list[str]]]] = {}
    while i < n:
        ln = lines[i]
        if len(ln) > 2000:
            raise DockprepError("bad_structure", "Structure file has an over-long line.", 422)
        s = ln.strip()
        if s == "loop_":
            i += 1
            cols: list[str] = []
            while i < n and lines[i].strip().startswith("_"):
                cols.append(lines[i].strip().split()[0])
                i += 1
            rows: list[list[str]] = []
            buf: list[str] = []
            while i < n:
                t = lines[i].strip()
                if t.startswith(("loop_", "_", "data_")) and not buf:
                    break
                if t.startswith(";"):  # multi-line text values are skipped as one token
                    i += 1
                    while i < n and not lines[i].startswith(";"):
                        i += 1
                    buf.append("?")
                else:
                    buf.extend(_cif_tokens(lines[i]))
                i += 1
                while cols and len(buf) >= len(cols):
                    rows.append(buf[: len(cols)])
                    buf = buf[len(cols) :]
                    if cols and cols[0] == "_atom_site.group_PDB" and len(rows) > MAX_ATOMS:
                        raise DockprepError("too_large", "Structure has too many atoms.", 413)
            if cols:
                loops[cols[0].split(".")[0]] = (cols, rows)
            continue
        if s.startswith("_") and "." in s:
            toks = _cif_tokens(s)
            if len(toks) >= 2:
                scalars[toks[0]] = toks[1]
            elif len(toks) == 1 and i + 1 < n and not lines[i + 1].startswith(";"):
                nxt = _cif_tokens(lines[i + 1])
                if nxt:
                    scalars[toks[0]] = nxt[0]
                    i += 1
        elif s.startswith("data_"):
            st.pdb_id = s[5:9].upper()
        i += 1
    st.expdta = scalars.get("_exptl.method", "")
    st.method_class = classify_method(st.expdta)
    for key, src in (
        ("_em_3d_reconstruction.resolution", "_em_3d_reconstruction.resolution"),
        ("_refine.ls_d_res_high", "_refine.ls_d_res_high"),
        ("_reflns.d_resolution_high", "_reflns.d_resolution_high"),
    ):
        v = scalars.get(key)
        if v and v not in "?.":
            try:
                st.resolution, st.resolution_source = float(v), src
                break
            except ValueError:
                pass
    if "_atom_site" not in loops:
        raise DockprepError("bad_structure", "No atom records found in structure file.", 422)
    cols, rows = loops["_atom_site"]
    ix = {c.split(".", 1)[1]: k for k, c in enumerate(cols)}
    need = ("group_PDB", "type_symbol", "Cartn_x", "Cartn_y", "Cartn_z")
    if any(k not in ix for k in need):
        raise DockprepError("bad_structure", "mmCIF atom_site table is incomplete.", 422)

    def g(row: list[str], *names: str, default: str = "") -> str:
        for nm in names:
            if nm in ix and row[ix[nm]] not in ("?", "."):
                return row[ix[nm]]
        return default

    first_model = None
    for row in rows:
        model = g(row, "pdbx_PDB_model_num", default="1")
        if first_model is None:
            first_model = model
        if model != first_model:
            continue
        try:
            st.atoms.append(
                Atom(
                    record="HETATM" if g(row, "group_PDB") == "HETATM" else "ATOM",
                    serial=int(g(row, "id", default="0")),
                    name=g(row, "auth_atom_id", "label_atom_id").strip('"'),
                    altloc=g(row, "label_alt_id"),
                    resname=g(row, "auth_comp_id", "label_comp_id"),
                    chain=g(row, "auth_asym_id", "label_asym_id", default="_"),
                    resseq=int(g(row, "auth_seq_id", "label_seq_id", default="0")),
                    icode=g(row, "pdbx_PDB_ins_code"),
                    x=_f(g(row, "Cartn_x")),
                    y=_f(g(row, "Cartn_y")),
                    z=_f(g(row, "Cartn_z")),
                    occ=_f(g(row, "occupancy", default="1")),
                    bfac=_f(g(row, "B_iso_or_equiv", default="0")),
                    element=g(row, "type_symbol", default="C").capitalize(),
                )
            )
        except ValueError:
            raise DockprepError("bad_structure", "Structure file has a malformed atom record.", 422) from None
    if "_pdbx_unobs_or_zero_occ_residues" in loops:
        c2, r2 = loops["_pdbx_unobs_or_zero_occ_residues"]
        j = {c.split(".", 1)[1]: k for k, c in enumerate(c2)}
        for row in r2:
            try:
                if row[j.get("polymer_flag", 0)] != "Y" and "polymer_flag" in j:
                    continue
                st.missing_residues.append(
                    (row[j["auth_asym_id"]], row[j["auth_comp_id"]], int(row[j["auth_seq_id"]]))
                )
            except (KeyError, ValueError, IndexError):
                continue
    return st


def parse_structure(data: bytes) -> Structure:
    """Detect format and parse. Rejects binary and empty content."""
    if b"\x00" in data[:65536]:
        raise DockprepError("bad_structure", "File is binary, not a PDB or mmCIF text file.", 415)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError:
            raise DockprepError("bad_structure", "File is not valid text.", 415) from None
    head = text.lstrip()[:2000]
    is_mmcif = head.startswith("data_") or "_atom_site." in text[:200000]
    st = parse_mmcif(text) if is_mmcif else parse_pdb(text)
    if not st.atoms:
        raise DockprepError("bad_structure", "No atom records found in structure file.", 422)
    return st


def fmt_pdb_atom(a: Atom, serial: int, altloc: str = "") -> str:
    name = a.name if len(a.name) >= 4 or len(a.element) > 1 else " " + a.name
    return (
        f"{a.record:<6}{serial % 100000:5d} {name:<4}{altloc[:1] or ' '}{a.resname:>3} {a.chain[:1]}"
        f"{a.resseq:4d}{a.icode[:1] or ' '}   {a.x:8.3f}{a.y:8.3f}{a.z:8.3f}{a.occ:6.2f}{a.bfac:6.2f}"
        f"          {a.element:>2}"
    )


def write_pdb(atoms: Iterable[Atom], remarks: Iterable[str] = ()) -> str:
    out = [f"REMARK   {r}"[:79] for r in remarks]
    prev = None
    k = 0
    for a in atoms:
        if prev is not None and a.chain != prev.chain and prev.record == "ATOM":
            k += 1
            out.append("TER")
        k += 1
        out.append(fmt_pdb_atom(a, k))
        prev = a
    out.append("TER")
    out.append("END")
    return "\n".join(out) + "\n"
