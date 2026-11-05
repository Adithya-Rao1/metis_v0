"""Wave 4 backend: project rename, compound/measurement/note delete, SMILES depiction for docking prep, the response
headers, the model card's higher_is_better, and the queue's worker ownership."""

import threading
import time
import uuid

import pytest
from api_support import H
from fastapi.testclient import TestClient

from backend.jobs import deps
from backend.jobs.queue import InProcessJobQueue, QuotaExceeded
from backend.schemas.interfaces import ModelCard


# ---------------------------------------------------------------- project rename
def test_rename_project_owner_only(client, project, su):
    r = client.patch(f"/projects/{project}", json={"name": "  Renamed  "}, headers=H("a"))
    assert r.status_code == 200 and r.json()["name"] == "Renamed" and r.json()["id"] == project
    assert client.get(f"/projects/{project}", headers=H("a")).json()["name"] == "Renamed"
    other = client.patch(f"/projects/{project}", json={"name": "stolen"}, headers=H("b"))
    miss = client.patch(f"/projects/{uuid.uuid4()}", json={"name": "x"}, headers=H("b"))
    assert (other.status_code, other.json()) == (miss.status_code, miss.json()) == (404, {"detail": "not found"})
    assert client.get(f"/projects/{project}", headers=H("a")).json()["name"] == "Renamed"
    assert su.execute("SELECT action, resource FROM audit_log WHERE action = 'project_rename'").fetchall() == [
        ("project_rename", f"project:{project}")
    ]


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "   "}, {"name": "a\nb"}, {"name": "x" * 201}, {"name": "ok", "owner_id": "x"}, {"name": None}])
def test_rename_validates(client, project, body):
    assert client.patch(f"/projects/{project}", json=body, headers=H("a")).status_code == 422
    assert client.get(f"/projects/{project}", headers=H("a")).json()["name"] == "P1"


# ---------------------------------------------------------------- deletes
def test_delete_compound_cascades_and_is_owner_only(client, loaded, su):
    pid, comps = loaded
    cid, keep = comps[0]["id"], comps[1]["id"]
    client.post(f"/projects/{pid}/measurements", json={"compound_id": cid, "endpoint": "logd", "value": 1.0}, headers=H("a"))
    assert client.delete(f"/projects/{pid}/compounds/{cid}", headers=H("b")).status_code == 404
    assert client.delete(f"/projects/{uuid.uuid4()}/compounds/{cid}", headers=H("a")).status_code == 404
    assert client.get(f"/projects/{pid}", headers=H("a")).json()["compounds"][0]["id"] == cid
    r = client.delete(f"/projects/{pid}/compounds/{cid}", headers=H("a"))
    assert r.status_code == 204 and r.content == b""
    assert [c["id"] for c in client.get(f"/projects/{pid}", headers=H("a")).json()["compounds"]] == [c["id"] for c in comps[1:]]
    assert su.execute("SELECT count(*) FROM measurement").fetchone()[0] == 0
    assert client.delete(f"/projects/{pid}/compounds/{cid}", headers=H("a")).status_code == 404  # already gone
    assert client.delete(f"/projects/{pid}/compounds/{keep}", headers=H("a")).status_code == 204
    rows = su.execute("SELECT action, resource FROM audit_log WHERE action = 'compound_delete' ORDER BY id").fetchall()
    assert rows == [("compound_delete", f"compound:{cid}"), ("compound_delete", f"compound:{keep}")]
    assert "CCO" not in str(rows)  # ids only


def test_compound_of_another_project_is_not_deletable_through_this_one(client, loaded):
    pid, comps = loaded
    other = client.post("/projects", json={"name": "other"}, headers=H("a")).json()["id"]
    assert client.delete(f"/projects/{other}/compounds/{comps[0]['id']}", headers=H("a")).status_code == 404
    assert len(client.get(f"/projects/{pid}", headers=H("a")).json()["compounds"]) == 3


def test_delete_measurement_owner_only_and_scoped_to_the_project(client, loaded, su):
    pid, comps = loaded
    m = client.post(f"/projects/{pid}/measurements", json={"compound_id": comps[0]["id"], "endpoint": "logd", "value": 2.5}, headers=H("a")).json()["id"]
    other = client.post("/projects", json={"name": "other"}, headers=H("a")).json()["id"]
    assert client.delete(f"/projects/{pid}/measurements/{m}", headers=H("b")).status_code == 404
    assert client.delete(f"/projects/{other}/measurements/{m}", headers=H("a")).status_code == 404
    assert su.execute("SELECT count(*) FROM measurement").fetchone()[0] == 1
    assert client.delete(f"/projects/{pid}/measurements/{m}", headers=H("a")).status_code == 204
    assert su.execute("SELECT count(*) FROM measurement").fetchone()[0] == 0
    assert client.delete(f"/projects/{pid}/measurements/{m}", headers=H("a")).status_code == 404
    assert su.execute("SELECT resource FROM audit_log WHERE action = 'measurement_delete'").fetchall() == [(f"measurement:{m}",)]


def test_delete_note_owner_only(client, project, su, users):
    owner = users["a"].id
    nid = su.execute("INSERT INTO note (owner_id, project_id, body) VALUES (%s, %s, 'secret note text') RETURNING id", (owner, project)).fetchone()[0]
    assert client.delete(f"/projects/{project}/notes/{nid}", headers=H("b")).status_code == 404
    assert su.execute("SELECT count(*) FROM note").fetchone()[0] == 1
    assert client.delete(f"/projects/{project}/notes/{nid}", headers=H("a")).status_code == 204
    assert su.execute("SELECT count(*) FROM note").fetchone()[0] == 0
    assert client.delete(f"/projects/{project}/notes/{nid}", headers=H("a")).status_code == 404
    assert "secret note text" not in str(su.execute("SELECT * FROM audit_log").fetchall())


def test_new_routes_need_authentication(client, project):
    for method, url in (
        ("PATCH", f"/projects/{project}"),
        ("DELETE", f"/projects/{project}/compounds/{uuid.uuid4()}"),
        ("DELETE", f"/projects/{project}/measurements/{uuid.uuid4()}"),
        ("DELETE", f"/projects/{project}/notes/{uuid.uuid4()}"),
        ("POST", "/dockprep/ligands/depiction"),
    ):
        assert client.request(method, url, json={"name": "x", "smiles": "CCO"}).status_code == 401, url


# ---------------------------------------------------------------- docking-prep depiction
def test_ligand_depiction_returns_an_inert_svg(client):
    r = client.post("/dockprep/ligands/depiction", json={"smiles": "CC(=O)Oc1ccccc1C(=O)O"}, headers=H("a"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.lstrip().startswith(("<?xml", "<svg")) and "<script" not in r.text.lower()
    assert r.headers["content-security-policy"].startswith("default-src 'none'") and r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("body,status", [({"smiles": "not a smiles ((("}, 422), ({"smiles": ""}, 422), ({"smiles": "C" * 2001}, 422), ({}, 422), ({"smiles": "CCO", "x": 1}, 422)])
def test_ligand_depiction_rejects_bad_input_without_echoing_it(client, body, status):
    r = client.post("/dockprep/ligands/depiction", json=body, headers=H("a"))
    assert r.status_code == status
    assert "not a smiles" not in r.text


# ---------------------------------------------------------------- headers
def test_authenticated_responses_are_no_store_and_public_models_stay_cacheable(client, project):
    for url in ("/projects", f"/projects/{project}", "/jobs/" + str(uuid.uuid4())):
        assert client.get(url, headers=H("a")).headers["cache-control"] == "no-store", url
    assert "cache-control" not in client.get("/health").headers
    assert "cache-control" not in client.get("/models").headers
    # whatever a route sets itself is kept and never doubled by the middleware
    r = client.get(f"/projects/{project}/export", params={"format": "csv"}, headers=H("a"))
    assert r.status_code == 200 and r.headers["cache-control"].count("no-store") == 1


def test_hsts_only_over_tls_and_only_trusted_proxy_headers_count(app):
    http = TestClient(app, base_url="http://testserver")
    https = TestClient(app, base_url="https://testserver")
    assert "strict-transport-security" not in http.get("/health").headers
    assert https.get("/health").headers["strict-transport-security"] == "max-age=63072000; includeSubDomains"
    assert "preload" not in https.get("/health").headers["strict-transport-security"]
    fwd = {"X-Forwarded-Proto": "https"}
    assert "strict-transport-security" not in http.get("/health", headers=fwd).headers  # not trusted by default
    from backend.accounts.config import AccountsConfig

    app.state.accounts_config = AccountsConfig(trust_proxy=True)
    assert "strict-transport-security" in http.get("/health", headers=fwd).headers
    assert "strict-transport-security" not in http.get("/health", headers={"X-Forwarded-Proto": "http"}).headers
    assert "strict-transport-security" in http.get("/health", headers={"X-Forwarded-Proto": "http, https"}).headers  # last hop


# ---------------------------------------------------------------- model card
def test_model_card_carries_higher_is_better_and_defaults_to_none(client):
    card = client.get("/models/logd").json()
    assert "higher_is_better" in card and card["higher_is_better"] is None
    from api_support import FakeRegistry

    base = FakeRegistry()._card("logd").model_dump()
    assert ModelCard(**{**base, "higher_is_better": False}).higher_is_better is False
    assert ModelCard(**{**base, "higher_is_better": True}).model_dump()["higher_is_better"] is True


# ---------------------------------------------------------------- queue: worker ownership and quota
def _insert_job(su, owner, pid, state="running", worker=None, beat="now()"):
    return su.execute(
        f"INSERT INTO job (owner_id, project_id, kind, state, worker_id, heartbeat_at) VALUES (%s, %s, 'predict', %s, %s, {beat}) RETURNING id",
        (owner, pid, state, worker),
    ).fetchone()[0]


def test_rows_of_a_live_foreign_worker_are_left_alone_and_stale_ones_are_failed(factory, users, loaded, su):
    pid, _ = loaded
    owner = users["a"].id
    live = _insert_job(su, owner, pid, worker=uuid.uuid4(), beat="now()")
    stale = _insert_job(su, owner, pid, worker=uuid.uuid4(), beat="now() - interval '5 minutes'")
    legacy = _insert_job(su, owner, pid, worker=None, beat="NULL")
    q = InProcessJobQueue(factory=factory)
    try:
        assert q.get(owner, live).state == "running"
        assert q.get(owner, stale).state == "failed" and q.get(owner, legacy).state == "failed"
    finally:
        q.shutdown()
    rows = dict(su.execute("SELECT id, state FROM job").fetchall())
    assert rows == {live: "running", stale: "failed", legacy: "failed"}


def test_stale_limit_is_configurable(factory, users, loaded, su):
    pid, _ = loaded
    owner = users["a"].id
    jid = _insert_job(su, owner, pid, worker=uuid.uuid4(), beat="now() - interval '3 seconds'")
    q = InProcessJobQueue(factory=factory, stale_s=1.0)
    try:
        assert q.get(owner, jid).state == "failed"
    finally:
        q.shutdown()


def test_heartbeat_keeps_a_long_job_alive_and_a_reaped_row_stays_failed(factory, users, loaded, su):
    pid, _ = loaded
    owner = users["a"].id
    gate = threading.Event()
    first = InProcessJobQueue(max_workers=1, factory=factory, heartbeat_s=0.2, stale_s=1.0)
    second = InProcessJobQueue(factory=factory, stale_s=1.0)
    try:
        rec = first.submit(owner, uuid.UUID(pid), 1, 0.0, lambda p: gate.wait(20))
        time.sleep(2.2)  # longer than stale_s: only the heartbeat keeps the row alive
        assert second.get(owner, rec.id).state in ("queued", "running")
        assert su.execute("SELECT worker_id FROM job WHERE id = %s", (rec.id,)).fetchone()[0] == first.worker_id
        # now the owner process "dies": stop its heartbeat; the row goes stale and the other process fails it
        first._stop.set()
        time.sleep(1.6)
        assert second.get(owner, rec.id).state == "failed"
        gate.set()
        first.wait(rec.id)
        # the late finish of the dead worker's job does not resurrect the row
        assert su.execute("SELECT state, error_code FROM job WHERE id = %s", (rec.id,)).fetchone() == ("failed", "interrupted_by_restart")
    finally:
        gate.set()
        first.shutdown()
        second.shutdown()


def test_quota_counts_running_jobs_of_a_deleted_project(factory, users, client, loaded):
    pid, _ = loaded
    owner = users["a"].id
    p2 = client.post("/projects", json={"name": "second"}, headers=H("a")).json()["id"]
    gate = threading.Event()
    q = InProcessJobQueue(max_workers=1, factory=factory)
    try:
        q.submit(owner, uuid.UUID(pid), 1, 0.0, lambda p: gate.wait(20), max_active=2)
        q.submit(owner, uuid.UUID(pid), 1, 0.0, lambda p: gate.wait(20), max_active=2)
        assert client.delete(f"/projects/{pid}", headers=H("a")).status_code == 204
        with pytest.raises(QuotaExceeded):
            q.submit(owner, uuid.UUID(p2), 1, 0.0, lambda p: None, max_active=2)
    finally:
        gate.set()
        q.shutdown()


def test_memory_only_queue_still_counts_per_owner():
    q = InProcessJobQueue(max_workers=1)
    gate = threading.Event()
    me, other = uuid.uuid4(), uuid.uuid4()
    try:
        q.submit(me, uuid.uuid4(), 1, 0.0, lambda p: gate.wait(20), max_active=1)
        with pytest.raises(QuotaExceeded):
            q.submit(me, uuid.uuid4(), 1, 0.0, lambda p: None, max_active=1)
        q.submit(other, uuid.uuid4(), 1, 0.0, lambda p: None, max_active=1)
    finally:
        gate.set()
        q.shutdown()


def test_the_heartbeat_thread_does_not_keep_an_abandoned_queue_alive(factory, users, loaded):
    import gc
    import weakref

    pid, _ = loaded
    q = InProcessJobQueue(factory=factory, heartbeat_s=0.05)
    q.submit(users["a"].id, uuid.UUID(pid), 1, 0.0, lambda p: None)
    q.wait(next(iter(q._futures)))
    beat = q._beat
    ref = weakref.ref(q)
    q.shutdown(wait=False)
    del q
    gc.collect()
    time.sleep(0.3)
    assert ref() is None and beat is not None and not beat.is_alive()


def test_get_job_route_still_works_through_the_override(client, loaded):
    pid, _ = loaded
    jid = client.post(f"/projects/{pid}/predict", json={"endpoints": ["logd"]}, headers=H("a")).json()["job_id"]
    assert client.get(f"/jobs/{jid}", headers=H("a")).status_code == 200
    assert deps.get_queue is not None
