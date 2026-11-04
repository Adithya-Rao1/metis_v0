# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310
import base64
import time

import pytest

from backend.dockprep import sandbox
from backend.dockprep.errors import DockprepError
from backend.dockprep.limits import sanitize_name
from backend.dockprep.pdbio import parse_structure
from backend.dockprep.sandbox import run_sandboxed


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


@pytest.mark.parametrize("data,status", [
    (b"\x00\x01\x02binary" * 100, 415),
    (b"\xff\xfe\xfa" * 500, 415),
    (b"", 422),
    (b"hello world\nnot a structure\n", 422),
    (b"ATOM      1  N   GLY A   1      nan     2.000   3.000  1.00 20.00           N\n", 422),
    (b"ATOM      1  N   GLY A   1       1e9     2.000   3.000  1.00 20.00           N\n", 422),
    (b"ATOM      1  N   GLY A   x       1.000   2.000   3.000  1.00 20.00           N\n", 422),
    (b"ATOM  " + b"A" * 1000 + b"\n", 422),
    (b"data_x\nloop_\n_atom_site.group_PDB\nATOM\n", 422),
])
def test_malformed_rejected_in_parser(data, status):
    with pytest.raises(DockprepError) as ei:
        parse_structure(data)
    assert ei.value.status == status
    assert "ATOM" not in ei.value.message and "nan" not in ei.value.message


def test_too_many_atoms(monkeypatch):
    monkeypatch.setattr("backend.dockprep.pdbio.MAX_ATOMS", 5)
    ln = "ATOM      1  N   GLY A   1       1.000   2.000   3.000  1.00 20.00           N\n"
    with pytest.raises(DockprepError) as ei:
        parse_structure((ln * 10).encode())
    assert ei.value.status == 413


def test_path_like_and_hostile_names_sanitized():
    for raw, want_bad in [("../../etc/passwd", "/"), ("..\\..\\win.ini", "\\"), ("a b;rm -rf /", " "), ("\x00evil", "\x00"), (".hidden", "."), ("", "/")]:
        s = sanitize_name(raw)
        assert want_bad not in s[:1] and "/" not in s and "\\" not in s and " " not in s and "\x00" not in s and not s.startswith(".")
    assert sanitize_name("x" * 500) == "x" * 40


def test_chain_ids_and_resnames_from_hostile_pdb_do_not_escape():
    # path-like chain/resname fields cannot be longer than the fixed columns; a long name is truncated by column slicing
    ln = "ATOM      1  N   ../ /   1       1.000   2.000   3.000  1.00 20.00           N\n"
    st = parse_structure((ln + ln.replace(" N  ", " CA ")).encode())
    assert all(len(a.resname) <= 3 and len(a.chain) <= 1 for a in st.atoms)
    from backend.dockprep.protein import prepare_protein

    r = prepare_protein(("\n".join([ln.replace(" N  ", n) for n in (" N  ", " CA ", " C  ")])).encode(), add_h=False)
    assert all("/" not in k for k in r["files"])


def test_sandbox_rejects_binary_and_hides_content():
    with pytest.raises(DockprepError) as ei:
        run_sandboxed("protein", {"data_b64": _b64(b"\x00" * 5000 + b"SECRET_STRUCTURE")})
    assert "SECRET" not in ei.value.message and ei.value.status == 415


def test_sandbox_huge_payload_limit_enforced_by_cpu_limit(monkeypatch):
    # a worker that spins is killed by the wall-clock timeout
    code = "import time\nwhile True: time.sleep(0.05)\n"
    import pathlib
    import tempfile

    d = tempfile.mkdtemp()
    p = pathlib.Path(d, "spin.py"); p.write_text(code)
    monkeypatch.setattr(sandbox, "_WORKER", str(p))
    t0 = time.time()
    with pytest.raises(DockprepError) as ei:
        run_sandboxed("protein", {}, timeout=2)
    assert ei.value.status == 504 and time.time() - t0 < 10


def test_sandbox_worker_crash_is_generic(monkeypatch, tmp_path):
    p = tmp_path / "crash.py"
    p.write_text("import sys\nprint('ATOM SECRET')\nsys.exit(3)\n")
    monkeypatch.setattr(sandbox, "_WORKER", str(p))
    with pytest.raises(DockprepError) as ei:
        run_sandboxed("protein", {})
    assert "SECRET" not in ei.value.message


def test_sandbox_concurrency_cap(monkeypatch):
    import threading

    sem = threading.BoundedSemaphore(1)
    sem.acquire()
    monkeypatch.setattr(sandbox, "_slots", sem)
    with pytest.raises(DockprepError) as ei:
        run_sandboxed("ligand", {"smiles": "CCO"})
    assert ei.value.status == 503


def test_content_not_executed_in_worker(tmp_path):
    marker = tmp_path / "pwned"
    evil = f"REMARK $(touch {marker}) __import__('os').system('touch {marker}')\nATOM      1  N   GLY A   1       1.000   2.000   3.000  1.00 20.00           N\n" * 30
    try:
        run_sandboxed("protein", {"data_b64": _b64(evil.encode()), "add_hydrogens": False})
    except DockprepError:
        pass
    assert not marker.exists()


def test_no_structure_content_in_logs(caplog):
    caplog.set_level("DEBUG")
    ln = "ATOM      1  N   GLY A   1       1.000   2.000   3.000  1.00 20.00           N\n"
    with pytest.raises(DockprepError):
        run_sandboxed("protein", {"data_b64": _b64(b"\x00" + ln.encode())})
    run_sandboxed("ligand", {"smiles": "CCOc1ccccc1N", "n_conformers": 1})
    text = " ".join(r.getMessage() + str(r.__dict__.get("op", "")) for r in caplog.records)
    assert "CCOc1" not in text and "ATOM" not in text
