"""Run prep operations on untrusted content in a time/CPU/memory-limited child interpreter.

The child is started with `python -I` (no env, no cwd/user site), an empty environment (PATH only) and a private
temporary cwd. Nothing from the input is executed; it is parsed as data. Logs record sizes and timings only.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess  # nosec B404 - fixed argv, no shell
import sys
import tempfile
import threading
import time
from pathlib import Path

from .errors import DockprepError
from .limits import (
    MAX_CONCURRENT_JOBS,
    OVERSIZE_BYTES,
    OVERSIZE_CPU_S,
    OVERSIZE_TIMEOUT_S,
    QUEUE_WAIT_S,
    SANDBOX_CPU_S,
    SANDBOX_MEM_BYTES,
    SANDBOX_TIMEOUT_S,
)

log = logging.getLogger("metis.dockprep")
_WORKER = str(Path(__file__).with_name("worker.py"))
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
MAX_RESULT_BYTES = 80 * 1024 * 1024


def budget_for(payload: dict, timeout: int = SANDBOX_TIMEOUT_S) -> tuple[int, int]:
    """(cpu_s, wall_s). Oversized structures get a smaller budget so one request cannot hold a slot for the full limit."""
    size = len(payload.get("data_b64") or "") * 3 // 4
    if size > OVERSIZE_BYTES:
        return min(SANDBOX_CPU_S, OVERSIZE_CPU_S), min(timeout, OVERSIZE_TIMEOUT_S)
    return SANDBOX_CPU_S, timeout


def run_sandboxed(op: str, payload: dict, timeout: int = SANDBOX_TIMEOUT_S) -> dict:
    cpu_s, timeout = budget_for(payload, timeout)
    if not _slots.acquire(timeout=QUEUE_WAIT_S):
        raise DockprepError("busy", "Docking prep is busy; try again shortly.", 503)
    t0 = time.monotonic()
    try:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "METIS_DP_CPU": str(cpu_s),
            "METIS_DP_MEM": str(SANDBOX_MEM_BYTES),
        }
        for k in ("METIS_PDB2PQR", "METIS_FPOCKET"):
            if k in os.environ:
                env[k] = os.environ[k]
        with tempfile.TemporaryDirectory(prefix="mdp_run_") as cwd:
            try:
                r = subprocess.run(  # noqa: S603 - fixed argv (-I worker), no shell, timeout, scrubbed env
                    [sys.executable, "-I", _WORKER],
                    input=json.dumps({"op": op, "payload": payload}).encode(),  # nosec B603
                    capture_output=True,
                    timeout=timeout,
                    cwd=cwd,
                    env=env,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                log.warning("dockprep sandbox timeout", extra={"op": op, "seconds": timeout})
                raise DockprepError("timeout", "Preparation exceeded the time limit.", 504) from None
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith(b"METIS_RESULT:")), None)
        if line is None or len(line) > MAX_RESULT_BYTES:
            log.warning("dockprep sandbox no result", extra={"op": op, "returncode": r.returncode})
            raise DockprepError("limit", "Preparation exceeded a resource limit or failed.", 422)
        msg = json.loads(line[len(b"METIS_RESULT:") :])
        if "error" in msg:
            e = msg["error"]
            raise DockprepError(e["code"], e["message"], int(e.get("status", 422)))
        log.info("dockprep done", extra={"op": op, "seconds": round(time.monotonic() - t0, 2)})
        return msg["ok"]
    finally:
        _slots.release()
