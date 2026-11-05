"""Change-log text rendering and zip bundling (in memory; nothing is stored)."""

from __future__ import annotations

import base64
import io
import json
import zipfile


def change_log_text(entries: list[dict], title: str = "Metis docking prep change log") -> str:
    out = [
        title,
        "=" * len(title),
        "Every step and parameter applied to the input. Experimental coordinates are only changed as listed.",
        "",
    ]
    for i, e in enumerate(entries, 1):
        head = f"{i:04d} [{e['step']}] {e['action']}"
        if e.get("target"):
            head += f" | {e['target']}"
        out.append(head)
        if e.get("detail"):
            out.append(f"       {e['detail']}")
        if e.get("params"):
            out.append("       params: " + json.dumps(e["params"], sort_keys=True, default=str))
    return "\n".join(out) + "\n"


def make_zip(
    files: dict[str, str], change_log: list[dict], report: dict, extra: dict[str, str] | None = None
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in {**files, **(extra or {})}.items():
            z.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), content)
        z.writestr("change_log.txt", change_log_text(change_log))
        z.writestr("change_log.json", json.dumps(change_log, indent=1, default=str))
        z.writestr("report.json", json.dumps(report, indent=1, default=str))
    return buf.getvalue()


def files_b64(files: dict[str, str]) -> dict[str, dict]:
    return {
        n: {"content_b64": base64.b64encode(c.encode()).decode(), "bytes": len(c.encode())}
        for n, c in files.items()
    }
