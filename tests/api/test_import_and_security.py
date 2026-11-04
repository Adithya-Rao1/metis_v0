import pathlib
import re
import subprocess
import sys


def test_router_mounts_on_main_app(client):
    from fastapi.testclient import TestClient

    from backend.main import create_app

    bare = TestClient(create_app())
    assert bare.get("/projects").status_code in (401, 422, 503)  # mounted (auth dependency fails closed)
    assert bare.get("/models").status_code in (200, 503)  # mounted, public (no 401)


def test_no_string_built_sql_in_my_packages():
    root = pathlib.Path(__file__).parents[2] / "backend"
    pat = re.compile(r"""(execute|text)\(\s*f["']""")
    for pkg in ("projects", "compounds", "predictions", "ranking", "exports", "jobs"):
        for f in (root / pkg).rglob("*.py"):
            assert not pat.search(f.read_text()), f


def test_worker_has_no_backend_imports_and_runs_isolated():
    w = pathlib.Path(__file__).parents[2] / "backend/compounds/worker.py"
    assert "import backend" not in w.read_text() and "from backend" not in w.read_text()
    out = subprocess.run([sys.executable, "-I", str(w)], input=b'{"op":"nope"}', capture_output=True, timeout=60, env={})
    assert b"bad_op" in out.stdout
