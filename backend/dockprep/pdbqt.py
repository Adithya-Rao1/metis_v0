"""PDBQT line formatting and a structural validator (own implementation; no Meeko/AutoDockTools)."""

from __future__ import annotations


def pdbqt_atom_line(
    serial: int,
    name: str,
    resname: str,
    chain: str,
    resseq: int,
    xyz: tuple[float, float, float],
    charge: float,
    adtype: str,
    record: str = "ATOM",
    occ: float = 1.0,
    bfac: float = 0.0,
    icode: str = "",
) -> str:
    nm = name if len(name) >= 4 else " " + name
    return (
        f"{record:<6}{serial % 100000:5d} {nm:<4} {resname[:3]:>3} {(chain or ' ')[:1]}{resseq % 10000:4d}"
        f"{icode[:1] or ' '}   {xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}{occ:6.2f}{bfac:6.2f}    "
        f"{charge:6.3f} {adtype:<2}"
    )


def parse_pdbqt_atoms(text: str) -> list[dict]:
    atoms = []
    for ln in text.splitlines():
        if ln.startswith(("ATOM", "HETATM")):
            atoms.append(
                {
                    "serial": int(ln[6:11]),
                    "name": ln[12:16].strip(),
                    "resname": ln[17:20].strip(),
                    "x": float(ln[30:38]),
                    "y": float(ln[38:46]),
                    "z": float(ln[46:54]),
                    "charge": float(ln[70:76]),
                    "type": ln[77:79].strip(),
                }
            )
    return atoms


def validate_ligand_pdbqt(text: str) -> dict:
    """Check ROOT/BRANCH nesting, serial references and TORSDOF. Raises ValueError on a defect."""
    lines = text.splitlines()
    serials: set[int] = set()
    stack: list[tuple[int, int]] = []
    branches = 0
    in_root = root_done = False
    tors = None
    for ln in lines:
        if ln.startswith("ROOT"):
            in_root = True
        elif ln.startswith("ENDROOT"):
            in_root, root_done = False, True
        elif ln.startswith(("ATOM", "HETATM")):
            if not (in_root or stack):
                raise ValueError("atom outside ROOT/BRANCH")
            s = int(ln[6:11])
            if s in serials:
                raise ValueError("duplicate serial")
            serials.add(s)
        elif ln.startswith("BRANCH"):
            if not root_done:
                raise ValueError("BRANCH before ENDROOT")
            _, a, b = ln.split()
            if int(a) not in serials:
                raise ValueError("BRANCH parent not emitted")
            stack.append((int(a), int(b)))
            branches += 1
        elif ln.startswith("ENDBRANCH"):
            _, a, b = ln.split()
            if not stack or stack.pop() != (int(a), int(b)):
                raise ValueError("unbalanced ENDBRANCH")
        elif ln.startswith("TORSDOF"):
            tors = int(ln.split()[1])
    if stack or not root_done:
        raise ValueError("unbalanced tree")
    if tors != branches:
        raise ValueError("TORSDOF mismatch")
    return {"atoms": len(serials), "torsdof": tors}
