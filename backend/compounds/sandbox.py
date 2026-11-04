"""Run the structure worker in a separate, time-limited, environment-stripped subprocess."""

from __future__ import annotations

import json
import pathlib
import subprocess  # nosec B404 - fixed argv, no shell, no user content in argv
import sys
import threading
from typing import Any

WORKER = pathlib.Path(__file__).with_name("worker.py")
TIMEOUT_S = 30
MAX_OUT_BYTES = 5_000_000
MAX_CONCURRENT = 4  # each worker may use ~3 GB address space; bounded like docking prep
SLOT_WAIT_S = 5.0  # wait this long for a free slot before 503 (a 10-row table asks for 10 pictures at once)
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


class WorkerError(RuntimeError):
    """Generic failure (timeout, crash, oversized output). Carries no structure content."""


class WorkerBusy(WorkerError):
    """All sandbox slots are in use; callers answer 503 and the client retries."""


def run(request: dict[str, Any], timeout: float = TIMEOUT_S) -> dict[str, Any]:
    payload = json.dumps(request).encode("utf-8")
    if not _slots.acquire(timeout=SLOT_WAIT_S):
        raise WorkerBusy("structure processing is busy")
    try:
        proc = subprocess.run(  # nosec B603 - argv is fixed; request goes over stdin only
            [sys.executable, "-I", str(WORKER)],
            input=payload,
            capture_output=True,
            timeout=timeout,
            env={},
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise WorkerError("structure processing timed out") from None
    finally:
        _slots.release()
    if proc.returncode != 0 or len(proc.stdout) > MAX_OUT_BYTES:
        raise WorkerError("structure processing failed")
    try:
        out = json.loads(proc.stdout.decode("utf-8"))
    except ValueError:
        raise WorkerError("structure processing failed") from None
    if not isinstance(out, dict):
        raise WorkerError("structure processing failed")
    return out
