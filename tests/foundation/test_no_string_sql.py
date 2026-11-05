"""Fail if backend/ (excluding migrations) builds SQL dynamically and passes it to execute()/text().

Grep-style (regex over source lines, multi-line calls joined) so it works without importing code.
Allowed: execute("literal ... %s", params), text("literal :param").
Forbidden argument forms: f-strings, "..." % x, "..." + x, "...".format(...), a bare variable assembled elsewhere
is not flagged (cannot be proven) but concatenation anywhere inside the call is.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
LEAD = ROOT / "leadstudio"

CALL = re.compile(r"\b(?:execute|executemany|exec_driver_sql|text|prepare)\s*\(")


def _calls(src: str):
    """Yield (lineno, argument-text) for each execute/text call, balancing parentheses across lines."""
    for m in CALL.finditer(src):
        depth, i = 1, m.end()
        while i < len(src) and depth:
            depth += {"(": 1, ")": -1}.get(src[i], 0)
            i += 1
        yield src.count("\n", 0, m.start()) + 1, src[m.end() : i - 1]


def violations(src: str) -> list[tuple[int, str]]:
    bad = []
    for line, arg in _calls(src):
        first = arg.strip()
        flat = " ".join(first.split())
        if (
            re.match(r"""^[rbRB]*[fF][rR]?["']""", first)
            or re.match(r"""^[rRbB]*["'].*["']\s*\.format\s*\(""", flat)
            or re.search(r"""["']\s*%\s*[\w(\[{]""", flat)
            or re.search(r"""["']\s*\+\s*\w""", flat)
            or re.search(r"""\w\s*\+\s*["']""", flat)
        ):
            bad.append((line, flat[:80]))
    return bad


def backend_files():
    for p in [*BACKEND.rglob("*.py"), *LEAD.rglob("*.py")]:
        if "migrations" in p.parts or "__pycache__" in p.parts:
            continue
        yield p


@pytest.mark.parametrize(
    "snippet",
    [
        'conn.execute(f"SELECT * FROM t WHERE id={x}")',
        'conn.execute("SELECT * FROM t WHERE id=%s" % x)',
        'conn.execute("SELECT * FROM t WHERE id=" + x)',
        'session.execute(text("SELECT " + col + " FROM t"))',
        'session.execute(text(f"SELECT {col}"))',
        'cur.execute("SELECT {}".format(x))',
        'cur.execute(\n    "SELECT a "\n    + x\n)',
    ],
)
def test_detector_catches_bad_patterns(snippet):
    assert violations(snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        'conn.execute("SELECT * FROM t WHERE id=%s", (x,))',
        'session.execute(text("SELECT * FROM t WHERE id=:i"), {"i": x})',
        "conn.execute(\"SELECT set_config('metis.user_id', %s, true)\", (uid,))",
        'session.execute(text("SELECT 1"))',
    ],
)
def test_detector_allows_parameterized(snippet):
    assert not violations(snippet), snippet


def test_backend_builds_no_sql_strings():
    found = []
    for p in backend_files():
        for line, arg in violations(p.read_text()):
            found.append(f"{p.relative_to(ROOT)}:{line}: {arg}")
    assert not found, "string-built SQL:\n" + "\n".join(found)
