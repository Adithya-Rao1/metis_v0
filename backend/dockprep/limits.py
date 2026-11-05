"""Size, time and name limits for hostile-input handling (threat model: hostile uploads)."""

from __future__ import annotations

import re

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # decoded structure file
MAX_BODY_BYTES = 22 * 1024 * 1024  # JSON request body (base64 inflates by 4/3)
MAX_ATOMS = 250_000
MAX_LINE_LEN = 400
MAX_SMILES_LEN = 2000
MAX_LIGAND_HEAVY_ATOMS = 120
MAX_CONFORMERS = 50
MAX_FETCH_BYTES = MAX_UPLOAD_BYTES
SANDBOX_TIMEOUT_S = 120
SANDBOX_CPU_S = 100
SANDBOX_MEM_BYTES = 3 * 1024**3
SUBPROCESS_TIMEOUT_S = 90  # pdb2pqr / fpocket inside the sandbox
FETCH_HOST_ALLOWLIST = frozenset({"files.rcsb.org"})
MAX_CONCURRENT_JOBS = 2
MAX_POCKET_GRID_CELLS = 6_000_000  # geometric pocket grid (1 A cells); ~180 A cube
QUEUE_WAIT_S = 3.0  # max wait for a sandbox slot before 503
OVERSIZE_BYTES = 2 * 1024 * 1024  # decoded structure above this gets the smaller budget
OVERSIZE_CPU_S = 30
OVERSIZE_TIMEOUT_S = 40

_NAME_OK = re.compile(r"[^A-Za-z0-9_.-]")
PDB_ID_RE = re.compile(r"^[0-9][A-Za-z0-9]{3}$")


def sanitize_name(raw: object, default: str = "item", max_len: int = 40) -> str:
    """Reduce any user-supplied label to [A-Za-z0-9_.-]; drops path components and leading dots."""
    s = str(raw or "")
    s = s.replace("\\", "/").split("/")[-1]
    s = _NAME_OK.sub("_", s).lstrip(".-_")[:max_len]
    return s or default
