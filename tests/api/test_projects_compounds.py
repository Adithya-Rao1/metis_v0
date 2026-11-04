import uuid

import pytest

from api_support import H


def test_project_crud_and_isolation(client):
    r = client.post("/projects", json={"name": "  Alpha  "}, headers=H("a"))
    assert r.status_code == 201 and r.json()["name"] == "Alpha"
    pid = r.json()["id"]
    assert [p["id"] for p in client.get("/projects", headers=H("a")).json()["projects"]] == [pid]
    assert client.get("/projects", headers=H("b")).json()["projects"] == []
    assert client.get(f"/projects/{pid}", headers=H("a")).status_code == 200
    assert client.get(f"/projects/{pid}", headers=H("b")).status_code == 404
    assert client.delete(f"/projects/{pid}", headers=H("b")).status_code == 404
    assert client.get(f"/projects/{pid}", headers=H("a")).status_code == 200  # survived B's delete
    assert client.delete(f"/projects/{pid}", headers=H("a")).status_code == 204
    assert client.get(f"/projects/{pid}", headers=H("a")).status_code == 404


@pytest.mark.parametrize("name", ["", "   ", "bad\x00name", "x" * 201])
def test_project_name_validation(client, name):
    assert client.post("/projects", json={"name": name}, headers=H("a")).status_code == 422


def test_unauthenticated_rejected(client, project):
    for method, path in [("get", "/projects"), ("post", "/projects"), ("get", f"/projects/{project}"), ("delete", f"/projects/{project}"),
                         ("get", f"/projects/{project}/export?format=csv"), ("post", f"/projects/{project}/predict"),
                         ("get", f"/jobs/{uuid.uuid4()}"), ("get", f"/predictions/{uuid.uuid4()}/uncertainty"),
                         ("put", f"/projects/{project}/ranking"), ("post", f"/projects/{project}/measurements"),
                         ("post", f"/projects/{project}/compounds")]:
        assert getattr(client, method)(path).status_code == 401, (method, path)


def test_add_smiles_standardizes(client, project):
    r = client.post(f"/projects/{project}/compounds", json={"smiles": ["CC(=O)[O-].[Na+]", "OC1=CC=CC=C1"]}, headers=H("a"))
    assert r.status_code == 201, r.text
    assert [c["canonical_smiles"] for c in r.json()["compounds"]] == ["CC(=O)O", "Oc1ccccc1"]


def test_cap_enforced_in_api_and_db(client, project, su, pg):
    ten = [f"C{'C' * i}O" for i in range(10)]
    assert client.post(f"/projects/{project}/compounds", json={"smiles": ten}, headers=H("a")).status_code == 201
    r = client.post(f"/projects/{project}/compounds", json={"smiles": ["CCN"]}, headers=H("a"))
    assert r.status_code == 422 and "10" in r.json()["detail"]
    r = client.post(f"/projects/{project}/compounds", json={"smiles": ten + ["CCN"]}, headers=H("a"))
    assert r.status_code == 422
    # DB trigger is the second layer: an INSERT that bypasses the API still fails.
    import psycopg

    with psycopg.connect(pg, autocommit=True) as c:
        owner = c.execute("SELECT owner_id FROM project WHERE id = %s", (project,)).fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO compound (owner_id, project_id, smiles, canonical_smiles) VALUES (%s, %s, 'CCN', 'CCN')", (owner, project))


def test_cap_counts_existing(client, project):
    nine = [f"C{'C' * i}O" for i in range(9)]
    assert client.post(f"/projects/{project}/compounds", json={"smiles": nine}, headers=H("a")).status_code == 201
    assert client.post(f"/projects/{project}/compounds", json={"smiles": ["CCN", "CCS"]}, headers=H("a")).status_code == 422
    assert client.post(f"/projects/{project}/compounds", json={"smiles": ["CCN"]}, headers=H("a")).status_code == 201


def test_errors_do_not_echo_structure(client, project):
    secret = "SECRETSTRUCT(((("
    r = client.post(f"/projects/{project}/compounds", json={"smiles": [secret, "CCO"]}, headers=H("a"))
    assert r.status_code == 422
    assert "SECRETSTRUCT" not in r.text
    assert r.json()["detail"]["problems"][0]["record"] == 1
    # nothing was added (all-or-nothing)
    assert client.get(f"/projects/{project}", headers=H("a")).json()["compounds"] == []


@pytest.mark.parametrize(
    "smi",
    ["", "C C", "C" * 2001, "[Fe]", "[Pt](Cl)(Cl)(N)N", "C" * 80, "CC|$_R1|", "C\x01C", "C1CC"],
)
def test_odd_inputs_rejected(client, project, smi):
    r = client.post(f"/projects/{project}/compounds", json={"smiles": [smi]}, headers=H("a"))
    assert r.status_code == 422, (smi[:10], r.text[:200])


def test_duplicates_rejected(client, project):
    assert client.post(f"/projects/{project}/compounds", json={"smiles": ["CCO", "OCC"]}, headers=H("a")).status_code == 422
    assert client.post(f"/projects/{project}/compounds", json={"smiles": ["CCO"]}, headers=H("a")).status_code == 201
    assert client.post(f"/projects/{project}/compounds", json={"smiles": ["OCC"]}, headers=H("a")).status_code == 422


def test_csv_upload(client, project):
    csv_text = "name,smiles\naspirin,CC(=O)Oc1ccccc1C(=O)O\n=cmd|evil,CCO\n"
    r = client.post(f"/projects/{project}/compounds", json={"format": "csv", "content": csv_text}, headers=H("a"))
    assert r.status_code == 201, r.text
    labels = [c["label"] for c in r.json()["compounds"]]
    assert labels == ["aspirin", "=cmd|evil"]  # stored verbatim; neutralized at export time


def test_csv_needs_header_and_cap(client, project):
    assert client.post(f"/projects/{project}/compounds", json={"format": "csv", "content": "CCO\nCCN"}, headers=H("a")).status_code == 422
    rows = "smiles\n" + "\n".join("C" * (i + 1) + "O" for i in range(11))
    r = client.post(f"/projects/{project}/compounds", json={"format": "csv", "content": rows}, headers=H("a"))
    assert r.status_code == 422


SDF = """ethanol
  test

  3  2  0  0  0  0  0  0  0  0999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.5000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.2000    1.2000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0
  2  3  1  0
M  END
$$$$
"""


def test_sdf_and_molblock_upload(client, project):
    r = client.post(f"/projects/{project}/compounds", json={"format": "sdf", "content": SDF}, headers=H("a"))
    assert r.status_code == 201, r.text
    assert r.json()["compounds"][0]["canonical_smiles"] == "CCO" and r.json()["compounds"][0]["label"] == "ethanol"
    r = client.post(f"/projects/{project}/compounds", json={"format": "molblock", "content": SDF.split("$$$$")[0].replace("O ", "N ")}, headers=H("a"))
    assert r.status_code == 201, r.text


def test_binary_and_garbage_uploads(client, project):
    for fmt, content in [("sdf", "\x00\x01binary"), ("sdf", "not an sdf"), ("csv", "a,b\n\x00"), ("molblock", "junk")]:
        r = client.post(f"/projects/{project}/compounds", json={"format": fmt, "content": content}, headers=H("a"))
        assert r.status_code == 422, (fmt, r.text)


def test_oversized_body_rejected(client, project):
    r = client.post(f"/projects/{project}/compounds", json={"format": "csv", "content": "smiles\n" + "C" * 1_100_000}, headers=H("a"))
    assert r.status_code in (413, 422)


def test_format_field_combinations(client, project):
    assert client.post(f"/projects/{project}/compounds", json={"format": "csv", "smiles": ["CCO"]}, headers=H("a")).status_code == 422
    assert client.post(f"/projects/{project}/compounds", json={"format": "smiles", "content": "CCO"}, headers=H("a")).status_code == 422
    assert client.post(f"/projects/{project}/compounds", json={"smiles": ["CCO"], "extra": 1}, headers=H("a")).status_code == 422


def test_worker_timeout_is_handled(client, project, monkeypatch):
    from backend.compounds import sandbox

    def boom(*a, **k):
        raise sandbox.WorkerError("structure processing timed out")

    monkeypatch.setattr(sandbox, "run", boom)
    r = client.post(f"/projects/{project}/compounds", json={"smiles": ["CCO"]}, headers=H("a"))
    assert r.status_code == 503


def test_sandbox_really_times_out(monkeypatch):
    from backend.compounds import sandbox

    monkeypatch.setattr(sandbox, "WORKER", __import__("pathlib").Path(__file__).with_name("slow_worker.py"))
    with pytest.raises(sandbox.WorkerError):
        sandbox.run({"op": "x"}, timeout=1)


def test_other_users_cannot_add_or_view_compounds(client, loaded):
    pid, comps = loaded
    cid = comps[0]["id"]
    assert client.post(f"/projects/{pid}/compounds", json={"smiles": ["CCN"]}, headers=H("b")).status_code == 404
    for suffix in ("conformer", "depiction.svg", "atoms"):
        assert client.get(f"/projects/{pid}/compounds/{cid}/{suffix}", headers=H("b")).status_code == 404
        assert client.get(f"/projects/{pid}/compounds/{cid}/{suffix}", headers=H("a")).status_code == 200


def test_conformer_depiction_atoms(client, loaded):
    pid, comps = loaded
    cid = comps[0]["id"]
    c = client.get(f"/projects/{pid}/compounds/{cid}/conformer", headers=H("a")).json()
    assert c["label"] == "computed" and "ETKDG" in c["method"] and "M  END" in c["molblock"]
    z = [float(line[20:30]) for line in c["molblock"].splitlines()[4:7]]
    assert any(abs(v) > 1e-3 for v in z)  # genuinely 3D
    s = client.get(f"/projects/{pid}/compounds/{cid}/depiction.svg?highlight=0&highlight=1", headers=H("a"))
    assert s.headers["content-type"].startswith("image/svg+xml") and "<svg" in s.text
    assert "default-src 'none'" in s.headers["content-security-policy"]
    assert client.get(f"/projects/{pid}/compounds/{cid}/depiction.svg?highlight=abc", headers=H("a")).status_code == 422
    atoms = client.get(f"/projects/{pid}/compounds/{cid}/atoms", headers=H("a")).json()["atoms"]
    assert [a["symbol"] for a in atoms] == ["C", "C", "O"] and [a["index"] for a in atoms] == [0, 1, 2]
    assert client.get(f"/projects/{pid}/compounds/{uuid.uuid4()}/atoms", headers=H("a")).status_code == 404


def test_measurements(client, loaded):
    pid, comps = loaded
    body = {"compound_id": comps[0]["id"], "endpoint": "logd", "value": 1.5, "unit": "log", "assay": "shake flask"}
    r = client.post(f"/projects/{pid}/measurements", json=body, headers=H("a"))
    assert r.status_code == 201 and r.json()["label"] == "measured"
    assert client.post(f"/projects/{pid}/measurements", json=body, headers=H("b")).status_code == 404
    assert client.post(f"/projects/{pid}/measurements", json={**body, "compound_id": str(uuid.uuid4())}, headers=H("a")).status_code == 404
    assert client.post(f"/projects/{pid}/measurements", json={**body, "value": "nan"}, headers=H("a")).status_code == 422
    d = client.get(f"/projects/{pid}", headers=H("a")).json()
    assert d["compounds"][0]["measurements"][0]["value"] == 1.5
    # cross-project compound id is not accepted
    p2 = client.post("/projects", json={"name": "P2"}, headers=H("a")).json()["id"]
    assert client.post(f"/projects/{p2}/measurements", json=body, headers=H("a")).status_code == 404


def test_delete_cascades(client, loaded, su):
    pid, _ = loaded
    assert client.delete(f"/projects/{pid}", headers=H("a")).status_code == 204
    assert su.execute("SELECT count(*) FROM compound").fetchone()[0] == 0
