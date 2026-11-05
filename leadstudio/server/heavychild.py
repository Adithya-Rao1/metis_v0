"""Worker process of the Lead Studio heavy-operation pool: `python -m leadstudio.server.heavychild`.

It parses molecules, checks SMARTS, embeds 3D structures and builds manual-mode previews, i.e. everything that runs
RDKit on text a user typed. One JSON request per line on stdin, one JSON answer per line on file descriptor 3. The
parent kills this process when a job takes too long or when it crashes, and replaces it after a number of jobs;
nothing in here is trusted to return. No SMILES is ever logged (stdout and stderr are /dev/null anyway).
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import sys
from collections.abc import Callable
from typing import Any

from leadstudio.server import procutil

IDLE_SECONDS = 600.0  # a worker nobody talks to for this long exits; the parent starts a new one on demand

_state: dict[str, Any] = {}


def _manual() -> Any:
    if "manual" not in _state:
        from leadstudio.server.core import Settings
        from leadstudio.server.manual import ManualService
        from leadstudio.server.stages import Services

        _state["manual"] = ManualService(Services(), None, Settings.from_env())
    return _state["manual"]


def _scorer() -> Any:
    if "scorer" not in _state:
        from leadstudio.scoring import RDKitScorer

        _state["scorer"] = RDKitScorer()
    return _state["scorer"]


# ------------------------------------------------------------------------------------------------ operations
def op_parse(args: dict) -> list[dict]:
    from leadstudio.server import molio

    out: list[dict] = []
    for item in args["items"]:
        try:
            mol = molio.parse_molecule(item.get("smiles"), item.get("molfile"))
            out.append({"ok": True, "canonical": molio.canonical(mol), "heavy": mol.GetNumHeavyAtoms()})
        except molio.InvalidMolecule as exc:
            out.append({"ok": False, "error": str(exc)})
    return out


def op_seed_block(args: dict) -> dict:
    """A client-supplied seed 3D block, checked against the seed SMILES: returns the block re-written in the seed's
    atom order, or a 422 answer if it is not the same molecule / has no 3D coordinates."""
    from fastapi import HTTPException

    from leadstudio.engine import geometry
    from leadstudio.schema import MAX_MOLFILE
    from leadstudio.server import molio

    block = args.get("molblock")
    if not isinstance(block, str) or len(block) > MAX_MOLFILE or "\x00" in block:
        raise HTTPException(422, "seed_molblock is not valid")
    block = block.replace("\r\n", "\n").split("$$$$")[0]
    if "M  END" not in block:
        raise HTTPException(422, "seed_molblock is not a MOL block")
    try:
        molio.parse_molecule(molfile=block)  # sanitize + the heavy-atom cap
    except molio.InvalidMolecule:
        raise HTTPException(422, "seed_molblock could not be read") from None
    res = geometry.seed_from_block(args["smiles"], block)
    if res is None:
        raise HTTPException(422, "seed_molblock must be 3D coordinates of the seed molecule")
    return {"molblock": res[0]}


def op_validate(args: dict) -> dict:
    from leadstudio.schema import RunConfig
    from leadstudio.server import molio

    try:
        mol = molio.parse_molecule(args.get("smiles"), args.get("molfile"))
    except molio.InvalidMolecule as exc:
        return {"ok": False, "error": str(exc)}
    smi, n = molio.canonical(mol), mol.GetNumHeavyAtoms()
    block, _provider = molio.molblock_3d(smi)
    if block is None:
        return {
            "ok": False,
            "canonical_smiles": smi,
            "heavy_atoms": n,
            "error": "Could not generate a 3D conformer for this molecule",
        }
    score = None
    if args.get("score"):
        try:
            score = _scorer().score(smi, RunConfig(seed_smiles=smi)).model_dump(mode="json")
        except Exception:
            score = None  # decoration only; the parent logs nothing about the molecule
    return {"ok": True, "canonical_smiles": smi, "heavy_atoms": n, "molblock": block, "score": score}


def op_applicable(args: dict) -> list[dict]:
    return _manual().applicable_local(args["smiles"], args["locked"])


def op_preview(args: dict) -> dict:
    from leadstudio.schema import ManualPreviewRequest

    req = ManualPreviewRequest.model_validate(args["request"])
    return _manual().preview_local(req).model_dump(mode="json")


def op_step_reward(args: dict) -> dict:
    """Validate both molecules of a manual step; optionally compute the server-side RDKit reward delta."""
    from leadstudio.schema import RunConfig

    svc = _manual()
    svc.check_smiles(args["parent"])
    svc.check_smiles(args["child"])
    if not args.get("reward"):
        return {"reward_delta": None}
    sc, cfg = svc.rdkit_scorer(), RunConfig(seed_smiles=args["parent"])
    return {"reward_delta": sc.score(args["child"], cfg).composite - sc.score(args["parent"], cfg).composite}


def op_smarts(args: dict) -> list[dict]:
    from leadstudio.profiles import check_smarts

    return [{"ok": (e := check_smarts(p)) is None, "error": e} for p in args["patterns"]]


OPS: dict[str, Callable[[dict], Any]] = {
    "parse": op_parse,
    "validate": op_validate,
    "seed_block": op_seed_block,
    "applicable": op_applicable,
    "preview": op_preview,
    "step_reward": op_step_reward,
    "smarts": op_smarts,
}


# ------------------------------------------------------------------------------------------------------ loop
def _answer(obj: dict) -> None:
    procutil.write_event_line(json.dumps(obj, separators=(",", ":")).encode())


def _preload() -> None:
    """Import and build what the jobs need now, so no job pays for it. Best effort: a job retries lazily."""
    with contextlib.suppress(Exception):
        from leadstudio.engine import geometry  # noqa: F401
        from leadstudio.server import molio  # noqa: F401

        svc = _manual()
        svc.registry()
        svc.rdkit_scorer()


def handle(line: bytes) -> dict:
    from fastapi import HTTPException

    try:
        req = json.loads(line)
        return {"ok": OPS[req["op"]](req.get("args") or {})}
    except HTTPException as exc:
        return {"http": [exc.status_code, str(exc.detail)]}
    except MemoryError:
        return {"error": "MemoryError"}
    except Exception as exc:
        return {"error": type(exc).__name__}


def serve() -> None:
    procutil.disable_core_dumps()
    mem = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    procutil.limit_address_space(mem)
    from rdkit import RDLogger

    RDLogger.DisableLog("rdApp.*")  # type: ignore[attr-defined]  # rdkit stubs omit this C++-backed name
    _preload()
    _answer({"ready": True})  # from here on the parent's per-job time limit applies to the job alone
    while True:
        ready, _, _ = select.select([0], [], [], IDLE_SECONDS)
        if not ready:
            break
        line = procutil.read_stdin_line()
        if line is None:
            break
        try:
            _answer(handle(line))
        except BrokenPipeError:
            break
    os._exit(0)


if __name__ == "__main__":
    serve()
