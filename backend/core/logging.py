"""Structured JSON logging with redaction.

No chemical structures, notes, credentials or personal data may reach log output. Redaction is applied
twice: by a logging.Filter (on the record, including args and extras) and again on the final JSON line.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import unicodedata
from typing import Any

REDACTED = "[REDACTED]"

SENSITIVE_KEYS = frozenset(
    {
        "smiles",
        "molblock",
        "mol",
        "molfile",
        "sdf",
        "inchi",
        "structure",
        "email",
        "password",
        "passwd",
        "token",
        "secret",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "note",
        "notes",
        "body",
        "pdb",
        "csv",
        "set_cookie",
    }
)

# Key names are matched by substring (token, secret, X-Api-Key, session_id, csrftoken, ...), not just exactly.
_SENSITIVE_SUBSTRINGS = (
    "token",
    "secret",
    "passw",
    "api_key",
    "apikey",
    "session",
    "csrf",
    "cookie",
    "auth",
    "jwt",
    "credential",
    "private_key",
    "smiles",
    "molblock",
    "molfile",
    "inchi",
    "email",
    "dsn",
)


def _is_sensitive_key(key: str) -> bool:
    k = key.lower().replace("-", "_")
    return k in SENSITIVE_KEYS or any(sub in k for sub in _SENSITIVE_SUBSTRINGS)


_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_MOLBLOCK = re.compile(r"(M {2}END|V[23]000|\${4}|^\s*\d+\s+\d+\s+\d+\s+\d+.*$)", re.M)
_INCHI = re.compile(r"InChI=\S+")
_KV = re.compile(r"(?<![\w\-])([\w\-]+)([\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_DSN = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@]*@")
_JWT = re.compile(r"\beyJ[\w\-]{5,}\.[\w\-]{5,}\.[\w\-]*")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060-\u2064\ufeff\u00ad]")
_BRACKET_ATOM = re.compile(r"\[[A-Za-z][A-Za-z0-9@+\-:]*\]")
_SMILES_CHARS = re.compile(r"^[BCNOPSFIHcnopsrl\[\]\(\)=#@+\-\\/%.0-9]+$")
_SMILES_RUN = re.compile(r"(?:\[[A-Za-z][A-Za-z0-9@+\-:]*\]|[BCNOPSFIHcnopsrl\[\]()=#@+\-\\/%.0-9])+")
_SMILES_STRUCT = re.compile(r"[\[\]\(\)=#@/\\%]|\d")
_TOKEN = re.compile(r"[^\s'\",:;{}]+")
_ATOM_RUN = re.compile(r"^[CNOPSFBIcnops]{6,}$")


def _smiles_like(core: str) -> bool:
    if len(core) < 5:
        return False
    structure = _BRACKET_ATOM.sub("C", core)  # bracket atoms ([Na+], [Se], [C@@H]) count as one atom
    if not _SMILES_CHARS.match(structure):
        return False
    atoms = sum(1 for ch in structure if ch in "BCNOPSFIcnops")
    # require real structure: atoms plus a bond/branch/ring/bracket char; plain numbers/versions excluded
    return atoms >= 2 and bool(_SMILES_STRUCT.search(core)) and not core.replace(".", "").isdigit()


def _looks_like_smiles(tok: str) -> bool:
    core = tok.strip(".,;:\"'")
    if len(core) < 5:
        return False
    return bool(_ATOM_RUN.match(core)) or _smiles_like(core)


def _scrub_token(tok: str) -> str:
    if _looks_like_smiles(tok):
        return REDACTED
    # SMILES glued to a non-SMILES prefix (mol_<smiles>, q=<smiles>): scan SMILES runs.
    return _SMILES_RUN.sub(lambda m: REDACTED if _smiles_like(m.group(0).strip(".,;:")) else m.group(0), tok)


def _scrub_kv(m: re.Match[str]) -> str:
    return f"{m.group(1)}{m.group(2)}{REDACTED}" if _is_sensitive_key(m.group(1)) else m.group(0)


def scrub_text(text: str) -> str:
    """Remove emails, molblocks, InChI, SMILES-like tokens, DSN passwords and credentials from free text."""
    text = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))
    if _MOLBLOCK.search(text):
        return REDACTED
    text = _DSN.sub(lambda m: f"{m.group(1)}{REDACTED}@", text)
    text = _JWT.sub(REDACTED, text)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    text = _KV.sub(_scrub_kv, text)
    text = _EMAIL.sub(REDACTED, text)
    text = _INCHI.sub(REDACTED, text)
    return _TOKEN.sub(lambda m: _scrub_token(m.group(0)), text)


def scrub_value(value: Any, key: str | None = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {k: scrub_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [scrub_value(v) for v in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return scrub_text(str(value))


_STD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            # scrub each argument by itself (keeps dict keys like smiles visible to key-based redaction)
            args = record.args
            record.args = scrub_value(args) if isinstance(args, dict) else tuple(scrub_value(a) for a in args)
        record.msg = scrub_text(str(record.msg))
        for k in list(record.__dict__):
            if k not in _STD_ATTRS:
                record.__dict__[k] = scrub_value(record.__dict__[k], k)
        if record.exc_info:
            # Exception text can embed SQL parameters or structures; keep the type only.
            record.exc_text = f"{record.exc_info[0].__name__ if record.exc_info[0] else 'Exception'}"
            record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _STD_ATTRS:
                payload[k] = v
        if record.exc_text:
            payload["exc"] = record.exc_text
        line = json.dumps(payload, default=str)
        # Defence in depth: scrub the serialized line too (handlers without the filter).
        return scrub_text(line) if _MOLBLOCK.search(line) or _EMAIL.search(line) or "://" in line else line


def configure_logging(level: str = "INFO", stream: Any = None) -> logging.Logger:
    """Install a single JSON handler with redaction on the root logger (idempotent)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_metis", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler._metis = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    # uvicorn installs non-redacting handlers (propagate=False); route them via the redacting root handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    return root
