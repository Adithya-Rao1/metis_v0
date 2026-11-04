# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310
"""Sandbox worker: runs ONE prep operation on untrusted input in a separate interpreter with resource limits.

Protocol: JSON on stdin {"op": ..., "payload": ...}; result as one line 'METIS_RESULT:<json>' on the real stdout.
Library chatter on stdout/stderr is discarded. Error messages are fixed strings (never structure content).
"""
import base64
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _limits() -> None:
    try:
        import resource

        cpu = int(os.environ.get("METIS_DP_CPU", "100"))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (200 * 1024 * 1024, 200 * 1024 * 1024))
        if sys.platform.startswith("linux"):   # RLIMIT_AS is not enforced reliably on macOS
            mem = int(os.environ.get("METIS_DP_MEM", str(3 * 1024**3)))
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    except Exception:  # noqa: BLE001 - limits are best effort on platforms lacking some rlimits
        return


def main() -> None:
    out = os.fdopen(os.dup(1), "w")
    devnull = open(os.devnull, "w")
    os.dup2(devnull.fileno(), 1)
    os.dup2(devnull.fileno(), 2)
    sys.stdout = sys.stderr = devnull
    _limits()
    sys.path.insert(0, ROOT)
    from backend.dockprep.errors import DockprepError

    try:
        req = json.loads(sys.stdin.read())
        op, p = req["op"], req["payload"]
        if op == "ligand":
            from backend.dockprep.ligand import prepare_ligand

            res = prepare_ligand(p.get("smiles"), p.get("sdf"), float(p.get("ph", 7.4)), int(p.get("n_conformers", 10)),
                                 int(p.get("seed", 42)), p.get("name", "ligand"), bool(p.get("all_conformers_sdf", False)))
        elif op == "protein":
            from backend.dockprep.protein import prepare_protein

            res = prepare_protein(base64.b64decode(p["data_b64"]), p.get("chains"), bool(p.get("remove_waters", True)),
                                  bool(p.get("remove_heteroatoms", True)), p.get("keep_hets"), bool(p.get("keep_metals", True)),
                                  bool(p.get("add_hydrogens", True)), float(p.get("ph", 7.4)), p.get("pdb_id"))
        elif op == "sites":
            from backend.dockprep.sites import analyze_sites

            res = analyze_sites(base64.b64decode(p["data_b64"]), p["modes"], p.get("residues"), p.get("ligand_id"), p.get("box"),
                                float(p.get("padding", 4.0)), p.get("chains"))
        else:
            raise DockprepError("bad_op", "Unknown operation.", 400)
        msg = {"ok": res}
    except DockprepError as e:
        msg = {"error": e.to_dict()}
    except MemoryError:
        msg = {"error": {"code": "limit", "message": "Resource limit exceeded.", "status": 413}}
    except Exception:  # noqa: BLE001 - never leak content through exception text
        msg = {"error": {"code": "prep_failed", "message": "Preparation failed on this input.", "status": 422}}
    out.write("METIS_RESULT:" + json.dumps(msg) + "\n")
    out.flush()


if __name__ == "__main__":
    main()
