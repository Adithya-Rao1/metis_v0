"""Sandboxed structure worker. Run as `python -I worker.py`: JSON request on stdin, JSON reply on stdout.

Never executes or evals input; applies CPU/file-size/address-space limits; no backend imports.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import resource
import sys


def _limit() -> None:
    for name, val in (
        ("RLIMIT_CPU", 20),
        ("RLIMIT_FSIZE", 0),
        ("RLIMIT_CORE", 0),
        ("RLIMIT_AS", 3 * 1024**3),
    ):
        try:
            resource.setrlimit(getattr(resource, name), (val, val))
        except (ValueError, OSError, AttributeError):  # e.g. RLIMIT_AS is not enforced on macOS
            pass


def _load():
    spec = importlib.util.spec_from_file_location(
        "metis_standardize", pathlib.Path(__file__).with_name("standardize.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    _limit()
    raw = sys.stdin.buffer.read(2_000_000 + 1)
    if len(raw) > 2_000_000:
        print(json.dumps({"error": "too_large"}))
        return 0
    try:
        req = json.loads(raw.decode("utf-8"))
        std = _load()
        op = req["op"]
        if op == "standardize":
            out = {
                "records": std.standardize_request(
                    req["format"], req.get("smiles"), req.get("content"), int(req["max_records"])
                )
            }
        elif op == "conformers":
            smi = req["smiles"]
            if not isinstance(smi, list) or len(smi) > 10:
                raise std.InputFormatError("bad request")
            out = {"conformers": [std.embed_3d(s) for s in smi]}
        elif op == "depict":
            out = std.depict(req["smiles"], [int(i) for i in req.get("highlight", [])][:200])
        else:
            out = {"error": "bad_op"}
    except Exception as exc:  # noqa: BLE001 - reply with a class name only, never content
        kind = type(exc).__name__
        out = {
            "error": "too_many"
            if kind == "TooManyRecords"
            else "format"
            if kind == "InputFormatError"
            else "failed",
            "detail": str(exc)[:100] if kind == "InputFormatError" else "",
        }
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
