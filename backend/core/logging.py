"""Structured JSON logging with redaction.

No chemical structures, notes, credentials or personal data may reach log output. Redaction is applied
twice: by a logging.Filter (on the record, including args and extras) and again on the final JSON line.
"""

from __future__ import annotations

import json
import logging
import re
import sys
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

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_MOLBLOCK = re.compile(r"(M {2}END|V[23]000|\${4}|^\s*\d+\s+\d+\s+\d+\s+\d+.*$)", re.M)
_INCHI = re.compile(r"InChI=\S+")
_KV = re.compile(
    r"(?i)\b("
    + "|".join(sorted(SENSITIVE_KEYS, key=len, reverse=True))
    + r")\b(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_SMILES_CHARS = re.compile(r"^[BCNOPSFIHcnopsrl\[\]\(\)=#@+\-\\/%.0-9]+$")
_SMILES_STRUCT = re.compile(r"[\[\]\(\)=#@/\\%]|\d")
_TOKEN = re.compile(r"[^\s'\",:;{}]+")
_ATOM_RUN = re.compile(r"^[CNOPSFBIcnops]{6,}$")


def _looks_like_smiles(tok: str) -> bool:
    core = tok.strip(".,;:\"'")
    if len(core) < 5:
        return False
    if _ATOM_RUN.match(core):
        return True
    if not _SMILES_CHARS.match(core):
        return False
    atoms = sum(1 for ch in core if ch in "BCNOPSFIcnops")
    # require real structure: atoms plus a bond/branch/ring/bracket char; plain numbers/versions excluded
    return atoms >= 2 and bool(_SMILES_STRUCT.search(core)) and not core.replace(".", "").isdigit()


def scrub_text(text: str) -> str:
    """Remove emails, molblocks, InChI, SMILES-like tokens and key=value secrets from free text."""
    if _MOLBLOCK.search(text):
        return REDACTED
    text = _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    text = _EMAIL.sub(REDACTED, text)
    text = _INCHI.sub(REDACTED, text)
    return _TOKEN.sub(lambda m: REDACTED if _looks_like_smiles(m.group(0)) else m.group(0), text)


def scrub_value(value: Any, key: str | None = None) -> Any:
    if key is not None and key.lower() in SENSITIVE_KEYS:
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
        return scrub_text(line) if _MOLBLOCK.search(line) or _EMAIL.search(line) else line


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
    return root
