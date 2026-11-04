# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310
import base64
import importlib
import io
import zipfile
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

router_mod = importlib.import_module("backend.dockprep.router")
from _fixtures import build_1m17_like

from backend.dockprep.auth import current_user
from backend.dockprep.fetch import MockFetcher, RcsbFetcher
from backend.dockprep.limits import MAX_BODY_BYTES
from backend.schemas.interfaces import UserCtx

USER = UserCtx(id=uuid4(), tier="free", email_verified=True)


def _app(fetcher=None, authed=True):
    from backend.dockprep import router

    app = FastAPI()
    app.include_router(router)
    if authed:
        app.dependency_overrides[current_user] = lambda: USER
    if fetcher is not None:
        app.dependency_overrides[router_mod.fetcher_provider] = lambda: (lambda: fetcher)
    return app


@pytest.fixture(scope="module")
def pdb_bytes():
    return build_1m17_like().encode()


def b64(b):
    return base64.b64encode(b).decode()


def test_requires_auth_fail_closed():
    c = TestClient(_app(authed=False))
    for path in ("ligands", "proteins", "sites"):
        assert c.post(f"/dockprep/{path}", json={}).status_code in (401, 403)


def test_main_mounts_router():
    from backend.main import create_app

    # unauthenticated call reaches our route (401 from the fail-closed dependency), not a 404
    assert TestClient(create_app()).post("/dockprep/ligands", json={}).status_code in (401, 403)


def test_ligands_json_and_zip():
    c = TestClient(_app())
    r = c.post("/dockprep/ligands", json={"smiles": "CCOc1ccccc1C(=O)O", "n_conformers": 2, "name": "my lig"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert set(j["files"]) == {"my_lig.pdbqt", "my_lig.sdf"} and j["change_log"] and "change log" in j["change_log_text"]
    assert base64.b64decode(j["files"]["my_lig.pdbqt"]["content_b64"]).startswith(b"REMARK")
    z = c.post("/dockprep/ligands?bundle=zip", json={"smiles": "CCO", "n_conformers": 1})
    assert z.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert {"change_log.txt", "change_log.json", "report.json", "ligand.pdbqt", "ligand.sdf"} <= set(names)


def test_ligand_bad_input_status_codes():
    c = TestClient(_app())
    assert c.post("/dockprep/ligands", json={"smiles": "((("}).status_code == 422
    assert c.post("/dockprep/ligands", json={"smiles": "CCO", "n_conformers": 999}).status_code == 422
    assert c.post("/dockprep/ligands", json={"smiles": "CCO", "extra": 1}).status_code == 422
    assert c.post("/dockprep/ligands", content=b"not json").status_code == 422


def test_proteins_by_pdb_id_with_mock_and_upload(pdb_bytes):
    f = MockFetcher({"1M17": pdb_bytes})
    c = TestClient(_app(f))
    r = c.post("/dockprep/proteins", json={"pdb_id": "1m17", "add_hydrogens": False})
    assert r.status_code == 200, r.text
    assert f.calls == ["1M17"]
    j = r.json()
    assert j["report"]["method_class"] == "xray" and "receptor.pdbqt" in j["files"]
    assert sum(1 for e in j["change_log"] if e["action"] == "water removed") == 12
    r2 = c.post("/dockprep/proteins?bundle=zip", json={"file_b64": b64(pdb_bytes), "filename": "../../x.pdb", "add_hydrogens": False})
    assert r2.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r2.content)).namelist()
    assert all(".." not in n and "/" not in n for n in names)


def test_proteins_rejections(pdb_bytes):
    f = MockFetcher({})
    c = TestClient(_app(f))
    assert c.post("/dockprep/proteins", json={"pdb_id": "../etc"}).status_code == 422
    assert c.post("/dockprep/proteins", json={"pdb_id": "9ZZZ"}).status_code == 404
    assert c.post("/dockprep/proteins", json={}).status_code == 422
    assert c.post("/dockprep/proteins", json={"pdb_id": "1M17", "file_b64": "AAAA"}).status_code == 422
    assert c.post("/dockprep/proteins", json={"file_b64": "!!!notb64"}).status_code == 422
    assert c.post("/dockprep/proteins", json={"file_b64": b64(b"\x00\x01" * 100)}).status_code == 415
    big = b"A" * (15 * 1024 * 1024 + 1)
    assert c.post("/dockprep/proteins", json={"file_b64": b64(big)}).status_code == 413
    huge = b'{"file_b64":"' + b"A" * (MAX_BODY_BYTES + 10) + b'"}'
    assert c.post("/dockprep/proteins", content=huge).status_code == 413


def test_fetch_disabled_by_default(monkeypatch):
    monkeypatch.delenv("METIS_DOCKPREP_FETCH", raising=False)
    c = TestClient(_app())
    assert c.post("/dockprep/proteins", json={"pdb_id": "1M17"}).status_code == 503


def test_sites_endpoint_with_vina_and_gnina_configs(pdb_bytes):
    c = TestClient(_app())
    r = c.post("/dockprep/sites", json={"file_b64": b64(pdb_bytes), "modes": ["ligand", "literature"], "engine": "vina", "exhaustiveness": 16})
    assert r.status_code == 200, r.text
    j = r.json()
    vina = base64.b64decode(j["files"]["vina.conf"]["content_b64"]).decode()
    assert "exhaustiveness = 16" in vina and "center_x" in vina and "box.txt" in j["files"]
    site = j["sites"][0]
    assert f"center_x = {site['center'][0]:.3f}" in vina
    g = c.post("/dockprep/sites", json={"file_b64": b64(pdb_bytes), "modes": ["ligand"], "engine": "gnina", "cnn_scoring": True}).json()
    assert "cnn_scoring = rescore" in base64.b64decode(g["files"]["gnina.conf"]["content_b64"]).decode()
    assert c.post("/dockprep/sites", json={"file_b64": b64(pdb_bytes), "modes": ["nonsense"]}).status_code == 422
    assert c.post("/dockprep/sites", json={"file_b64": b64(pdb_bytes), "modes": ["residues"], "residues": ["A:99999"]}).status_code == 422


def test_error_bodies_do_not_echo_input():
    c = TestClient(_app())
    r = c.post("/dockprep/proteins", json={"file_b64": b64(b"SECRETCONTENT")})
    assert "SECRETCONTENT" not in r.text


def test_rcsb_fetcher_allowlist_and_limits():
    seen = {}

    def get(url, max_bytes, timeout):
        seen["url"] = url
        return b"x" * 10

    f = RcsbFetcher(get)
    assert f.fetch_pdb("1m17") == b"x" * 10 and seen["url"] == "https://files.rcsb.org/download/1M17.pdb"
    from backend.dockprep.errors import DockprepError

    with pytest.raises(DockprepError):
        f.fetch_pdb("1M17/../../x")
    with pytest.raises(DockprepError):
        RcsbFetcher(lambda u, m, t: b"x" * 50, max_bytes=10).fetch_pdb("1M17")
