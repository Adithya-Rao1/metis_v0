# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310
"""Vina and GNINA config writers. Text only: nothing is executed. All values are validated and names sanitized."""

from __future__ import annotations

import math

from .errors import DockprepError
from .limits import sanitize_name

DEFAULTS = {"exhaustiveness": 8, "num_modes": 9, "energy_range": 3, "seed": 0}


def _check_box(center: list[float], size: list[float]) -> None:
    if len(center) != 3 or len(size) != 3 or not all(math.isfinite(v) for v in (*center, *size)):
        raise DockprepError("bad_param", "Box center and size need three finite numbers each.", 422)
    if any(abs(c) > 1e4 for c in center) or any(not (1.0 <= s <= 100.0) for s in size):
        raise DockprepError("bad_param", "Box size must be between 1 and 100 A per axis.", 422)


def _common(center, size, receptor, ligand, out, exhaustiveness, num_modes, energy_range, seed) -> list[str]:
    _check_box(center, size)
    if (
        not (1 <= int(exhaustiveness) <= 128)
        or not (1 <= int(num_modes) <= 50)
        or not (0 < float(energy_range) <= 10)
    ):
        raise DockprepError("bad_param", "Docking parameter out of range.", 422)
    return [
        f"receptor = {sanitize_name(receptor, 'receptor.pdbqt', 80)}",
        f"ligand = {sanitize_name(ligand, 'ligand.pdbqt', 80)}",
        "",
        f"center_x = {center[0]:.3f}",
        f"center_y = {center[1]:.3f}",
        f"center_z = {center[2]:.3f}",
        f"size_x = {size[0]:.3f}",
        f"size_y = {size[1]:.3f}",
        f"size_z = {size[2]:.3f}",
        "",
        f"exhaustiveness = {int(exhaustiveness)}",
        f"num_modes = {int(num_modes)}",
        f"energy_range = {float(energy_range):g}",
        f"seed = {int(seed)}",
        f"out = {sanitize_name(out, 'out', 80)}",
    ]


def vina_config(
    center,
    size,
    receptor="receptor.pdbqt",
    ligand="ligand.pdbqt",
    out="vina_out.pdbqt",
    exhaustiveness=DEFAULTS["exhaustiveness"],
    num_modes=DEFAULTS["num_modes"],
    energy_range=DEFAULTS["energy_range"],
    seed=DEFAULTS["seed"],
) -> str:
    lines = [
        "# AutoDock Vina config written by Metis dockprep. Run: vina --config vina.conf",
        *_common(center, size, receptor, ligand, out, exhaustiveness, num_modes, energy_range, seed),
    ]
    return "\n".join(lines) + "\n"


def gnina_config(
    center,
    size,
    receptor="receptor.pdbqt",
    ligand="ligand.sdf",
    out="gnina_out.sdf",
    exhaustiveness=DEFAULTS["exhaustiveness"],
    num_modes=DEFAULTS["num_modes"],
    energy_range=DEFAULTS["energy_range"],
    seed=DEFAULTS["seed"],
    cnn_scoring: bool = False,
) -> str:
    """cnn_scoring=False writes 'cnn_scoring = none' (Vina-style scoring only). True writes 'rescore' (GNINA default:
    CNN rescoring of final poses); the CNN is slow on CPU and is flagged in the file."""
    body = _common(center, size, receptor, ligand, out, exhaustiveness, num_modes, energy_range, seed)
    lines = [
        "# GNINA config written by Metis dockprep. Run: gnina --config gnina.conf",
        "# CNN scoring flag: "
        + (
            "ON (cnn_scoring = rescore). CNN scores are predictions from a learned model; slow without a GPU."
            if cnn_scoring
            else "OFF (cnn_scoring = none)"
        ),
        *body,
        f"cnn_scoring = {'rescore' if cnn_scoring else 'none'}",
    ]
    return "\n".join(lines) + "\n"
