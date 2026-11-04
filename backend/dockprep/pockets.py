# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310
"""Pocket detection. (1) fpocket CLI wrapper (GPL-2.0 binary; run as a separate process, never linked).
(2) A simple geometric buriedness finder in pure numpy, clearly labelled as a fallback: it is not fpocket and
its score is its own (pocket volume weighted by mean buriedness)."""
from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed argv, timeout, no shell
import tempfile
from pathlib import Path

import numpy as np

from .limits import SUBPROCESS_TIMEOUT_S
from .pdbio import Atom, write_pdb

_DIRS = np.array([d for d in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1),
                              *[(a, b, c) for a in (-1, 1) for b in (-1, 1) for c in (-1, 1)])], dtype=float)
GEOMETRIC_METHOD = "metis-geometric-v1"
GEOMETRIC_NOTE = ("Grid-based buriedness search (fallback, not fpocket): 1.0 A grid, points 3.0 A or more from any heavy atom, "
                  "14 rays of 10 A, point kept if at least 9 rays hit protein; connected points form a pocket. "
                  "Score = pocket volume in A^3 x mean buriedness fraction.")


def _stamp(idx: np.ndarray, shape: tuple[int, int, int], radius_cells: int, r2_cells: float) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    rng = range(-radius_cells, radius_cells + 1)
    for dx in rng:
        for dy in rng:
            for dz in rng:
                if dx * dx + dy * dy + dz * dz <= r2_cells:
                    p = idx + (dx, dy, dz)
                    ok = ((p >= 0) & (p < shape)).all(axis=1)
                    q = p[ok]
                    mask[q[:, 0], q[:, 1], q[:, 2]] = True
    return mask


def geometric_pockets(atoms: list[Atom], min_points: int = 30, max_pockets: int = 10, min_hits: int = 9) -> list[dict]:
    heavy = np.array([[a.x, a.y, a.z] for a in atoms if not a.is_h], dtype=float)
    if len(heavy) < 20:
        return []
    g = 1.0
    origin = heavy.min(axis=0) - 12.0
    dims = tuple(int(x) + 1 for x in (heavy.max(axis=0) + 12.0 - origin) / g)
    if dims[0] * dims[1] * dims[2] > 40_000_000:
        return []
    idx = np.rint((heavy - origin) / g).astype(int)
    solid = _stamp(idx, dims, 2, 4.0)           # protein body: within 2.0 A of an atom centre
    near = _stamp(idx, dims, 3, 9.0)            # too close for a ligand atom: within 3.0 A
    free = ~near
    cand = np.argwhere(free)
    # axis rays first (cheap pre-filter), then the 8 diagonals
    hits = np.zeros(len(cand), dtype=np.int16)
    keep = np.arange(len(cand))
    for d_i, d in enumerate(_DIRS):
        if d_i == 6:  # after axis rays, drop points with < 3 axis hits
            m = hits[keep] >= 3
            keep = keep[m]
        step = np.rint(d / np.linalg.norm(d) * np.arange(1, 11)[:, None]).astype(int) if d_i >= 6 else (d * np.arange(1, 11)[:, None]).astype(int)
        pts = cand[keep]
        hit = np.zeros(len(pts), dtype=bool)
        for s in step:
            q = pts + s
            ok = ((q >= 0) & (q < dims)).all(axis=1)
            sel = np.zeros(len(pts), dtype=bool)
            qq = q[ok]
            sel[ok] = solid[qq[:, 0], qq[:, 1], qq[:, 2]]
            hit |= sel
        hits[keep] += hit.astype(np.int16)
    good = keep[hits[keep] >= min_hits]
    pts = cand[good]
    if len(pts) == 0:
        return []
    bur = hits[good] / 14.0
    index = {tuple(p): i for i, p in enumerate(pts)}
    label = -np.ones(len(pts), dtype=int)
    nbrs = [(a, b, c) for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1) if (a, b, c) != (0, 0, 0)]
    n_lab = 0
    for i in range(len(pts)):
        if label[i] >= 0:
            continue
        label[i] = n_lab
        stack = [i]
        while stack:
            u = stack.pop()
            pu = pts[u]
            for d in nbrs:
                j = index.get((pu[0] + d[0], pu[1] + d[1], pu[2] + d[2]))
                if j is not None and label[j] < 0:
                    label[j] = n_lab
                    stack.append(j)
        n_lab += 1
    out = []
    for lab in range(n_lab):
        m = label == lab
        if m.sum() < min_points:
            continue
        xyz = pts[m] * g + origin
        out.append({
            "points": xyz, "n_points": int(m.sum()), "volume_A3": float(m.sum() * g ** 3), "mean_buriedness": float(bur[m].mean()),
            "score": float(m.sum() * g ** 3 * bur[m].mean()), "center": xyz.mean(axis=0).tolist(),
            "bbox_min": xyz.min(axis=0).tolist(), "bbox_max": xyz.max(axis=0).tolist(),
        })
    out.sort(key=lambda p: -p["score"])
    return out[:max_pockets]


def lining_residues(atoms: list[Atom], points: np.ndarray, cutoff: float = 4.5) -> list[str]:
    heavy = [a for a in atoms if not a.is_h and a.record == "ATOM"]
    if not heavy or len(points) == 0:
        return []
    xyz = np.array([[a.x, a.y, a.z] for a in heavy])
    sub = points[:: max(1, len(points) // 400)]
    res: set[tuple] = set()
    for s in range(0, len(xyz), 500):
        d = np.linalg.norm(xyz[s:s + 500, None, :] - sub[None, :, :], axis=2).min(axis=1)
        for k in np.nonzero(d <= cutoff)[0]:
            a = heavy[s + k]
            res.add((a.chain, a.resseq, a.icode, a.resname))
    return [f"{c}:{n}{ic} {rn}" for c, n, ic, rn in sorted(res)]


# ---------------- fpocket ----------------
def find_fpocket() -> str | None:
    env = os.environ.get("METIS_FPOCKET")
    if env and Path(env).is_file():
        return env
    return shutil.which("fpocket")


def parse_fpocket_info(info_text: str) -> list[dict]:
    """Parse '<name>_info.txt': blocks 'Pocket N :' followed by 'Key : value' lines."""
    pockets: list[dict] = []
    cur: dict | None = None
    for ln in info_text.splitlines():
        s = ln.strip()
        if s.startswith("Pocket ") and s.endswith(":"):
            try:
                cur = {"rank": int(s.split()[1])}
            except ValueError:
                cur = None
                continue
            pockets.append(cur)
        elif cur is not None and ":" in s:
            k, v = s.split(":", 1)
            try:
                cur[k.strip().lower().replace(" ", "_")] = float(v.strip())
            except ValueError:
                pass
    return pockets


def parse_fpocket_vertices(pqr_text: str) -> np.ndarray:
    pts = []
    for ln in pqr_text.splitlines():
        if ln.startswith(("ATOM", "HETATM")):
            try:
                pts.append((float(ln[30:38]), float(ln[38:46]), float(ln[46:54])))
            except ValueError:
                continue
    return np.array(pts, dtype=float).reshape(-1, 3)


def run_fpocket(atoms: list[Atom], timeout: int = SUBPROCESS_TIMEOUT_S) -> list[dict] | None:
    exe = find_fpocket()
    if exe is None:
        return None
    with tempfile.TemporaryDirectory(prefix="mdf_") as td:
        Path(td, "prot.pdb").write_text(write_pdb([a for a in atoms if a.record == "ATOM"]))
        try:
            r = subprocess.run([exe, "-f", "prot.pdb"], cwd=td, capture_output=True, timeout=timeout, check=False,  # nosec B603
                               env={"PATH": os.environ.get("PATH", ""), "HOME": td})
        except subprocess.TimeoutExpired:
            return None
        out = Path(td, "prot_out")
        info = out / "prot_info.txt"
        if r.returncode != 0 or not info.exists():
            return None
        pockets = parse_fpocket_info(info.read_text())
        for p in pockets:
            v = out / "pockets" / f"pocket{p['rank']}_vert.pqr"
            if v.exists():
                pts = parse_fpocket_vertices(v.read_text())
                if len(pts):
                    p["points"] = pts
                    p["center"] = pts.mean(axis=0).tolist()
                    p["bbox_min"], p["bbox_max"] = pts.min(axis=0).tolist(), pts.max(axis=0).tolist()
        return [p for p in pockets if "center" in p]
