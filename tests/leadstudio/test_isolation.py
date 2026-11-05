"""Cross-user isolation: user B can never read, stream, cancel or save over user A's objects. Every foreign object
answers 404 (never 403, never the object), and nothing of A's changes after B's attempts."""
from __future__ import annotations

import json
import uuid

import pytest
from conftest import H, make_app
from fastapi.testclient import TestClient

from leadstudio.server.core import Settings

NITRO = "CCOC(=O)c1ccc(cc1)[N+](=O)[O-]"
ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
PROFILE = {"name": "mine", "objectives": {}, "avoid_smarts": ["c1ccccc1"], "edit_allowlist": []}


@pytest.fixture
def slow(factory, users):
    """Mock runs take ~1 s so they are still in flight when B pokes at them."""
    return TestClient(make_app(factory, users, Settings(force_mock=True, mock_delay=0.5, replay_delay=0.0,
                                                        runs_per_minute=1000)))


def test_run_events_cancel_are_owner_only(slow):
    rid = slow.post("/lead/api/runs", json={"seed_smiles": NITRO}, headers=H("a")).json()["run_id"]
    assert slow.get(f"/lead/api/runs/{rid}/events", headers=H("b")).status_code == 404
    assert slow.delete(f"/lead/api/runs/{rid}", headers=H("b")).status_code == 404
    # A's run is untouched by B's attempted cancel and A can still stream it to the end
    r = slow.get(f"/lead/api/runs/{rid}/events", headers=H("a"))
    assert r.status_code == 200 and "event: run_end" in r.text and "event: error" not in r.text
    # unknown id and foreign id are indistinguishable
    a = slow.get(f"/lead/api/runs/{rid}/events", headers=H("b"))
    b = slow.get("/lead/api/runs/doesnotexist/events", headers=H("b"))
    assert (a.status_code, a.json()) == (b.status_code, b.json())


def test_run_manager_lookup_is_keyed_by_user(factory, users):
    st = make_app(factory, users).state.lead
    cfg = __import__("leadstudio.schema", fromlist=["RunConfig"]).RunConfig(seed_smiles="CCO")
    run = st.mgr.submit(users["a"].id, cfg, "runid1")
    assert st.mgr.get("runid1", users["a"].id) is run
    assert st.mgr.get("runid1", users["b"].id) is None
    assert st.mgr.get("runid1", uuid.uuid4()) is None


def test_second_user_cannot_use_first_users_replay_cache(factory, users):
    """Replay caches are per user: B running the same config never gets A's cached events (or even a hint of them)."""
    st = make_app(factory, users).state.lead
    from leadstudio.schema import RunConfig
    from leadstudio.server.core import config_key
    cfg = RunConfig(seed_smiles="CCO")
    assert config_key(users["a"].id, "CCO", cfg) != config_key(users["b"].id, "CCO", cfg)
    assert st.mgr.cache == {}


def test_stream_cap_is_per_user(factory, users):
    st = make_app(factory, users, Settings(force_mock=True, per_user_streams=1)).state.lead
    assert st.mgr.open_stream(users["a"].id)
    assert st.mgr.open_stream(users["b"].id)   # B is not starved by A's open stream


def test_library_runs_are_owner_only(client):
    rid = client.post("/lead/api/runs", json={"seed_smiles": NITRO}, headers=H("a")).json()["run_id"]
    evs = [json.loads(ln[6:]) for ln in client.get(f"/lead/api/runs/{rid}/events", headers=H("a")).text.splitlines()
           if ln.startswith("data: ")]
    saved = client.post("/lead/library/runs", json={"events": evs}, headers=H("a")).json()
    sid = saved["id"]
    assert client.get("/lead/library/runs", headers=H("b")).json() == {"runs": []}
    assert client.get(f"/lead/library/runs/{sid}", headers=H("b")).status_code == 404
    assert client.delete(f"/lead/library/runs/{sid}", headers=H("b")).status_code == 404
    assert client.get(f"/lead/library/runs/{sid}", headers=H("a")).status_code == 200  # survived B's delete


def test_library_manual_is_owner_only(client):
    body = {"steps": [{"parent_smiles": NITRO, "child_smiles": ASPIRIN, "edit_id": "x", "site": [0]}]}
    sid = client.post("/lead/library/manual", json=body, headers=H("a")).json()["id"]
    assert client.get("/lead/library/manual", headers=H("b")).json() == {"sessions": []}
    assert client.get(f"/lead/library/manual/{sid}", headers=H("b")).status_code == 404
    assert client.delete(f"/lead/library/manual/{sid}", headers=H("b")).status_code == 404
    assert len(client.get(f"/lead/library/manual/{sid}", headers=H("a")).json()["steps"]) == 1


def test_profiles_are_owner_only(client):
    pid = client.post("/lead/profiles", json=PROFILE, headers=H("a")).json()["id"]
    assert client.get("/lead/profiles", headers=H("b")).json() == {"profiles": []}
    assert client.get(f"/lead/profiles/{pid}", headers=H("b")).status_code == 404
    assert client.put(f"/lead/profiles/{pid}", json={**PROFILE, "name": "hijack"}, headers=H("b")).status_code == 404
    assert client.delete(f"/lead/profiles/{pid}", headers=H("b")).status_code == 404
    assert client.get(f"/lead/profiles/{pid}", headers=H("a")).json()["name"] == "mine"
    # B cannot attach A's profile to a run either
    r = client.post("/lead/api/runs", json={"seed_smiles": NITRO, "profile_id": pid}, headers=H("b"))
    assert r.status_code == 404


def test_manual_live_sessions_are_owner_only(client):
    sid = client.post("/lead/api/manual/sessions", json={"seed_smiles": NITRO}, headers=H("a")).json()["session_id"]
    step = {"parent_smiles": NITRO, "child_smiles": ASPIRIN, "edit_id": "x", "site": [0], "reward_delta": 0.0}
    assert client.post(f"/lead/api/manual/sessions/{sid}/steps", json=step, headers=H("b")).status_code == 404
    assert client.get(f"/lead/api/manual/sessions/{sid}/export", headers=H("b")).status_code == 404
    assert client.post(f"/lead/api/manual/sessions/{sid}/steps", json=step, headers=H("a")).status_code == 200


def test_pose_jobs_are_owner_only(factory, users):
    pm = make_app(factory, users).state.lead.poses
    egfr_drug = next(t for t in make_app(factory, users).state.lead.svc.get_target("egfr").drugs)
    job = None
    from leadstudio.server import molio
    smi = molio.canonical(molio.parse_molecule(egfr_drug.smiles))
    if pm.has_precomputed("egfr", smi):
        job = pm.submit(users["a"].id, "egfr", smi)
    assert job is not None and job.status == "done" and job.source == "precomputed"
    assert pm.get(job.job_id, users["a"].id) is not None
    assert pm.get(job.job_id, users["b"].id) is None


def test_pose_http_flow_is_owner_only(client):
    egfr = client.get("/lead/api/targets/egfr", headers=H("a")).json()
    found = None
    for d in egfr["drugs"]:
        r = client.post("/lead/api/poses", json={"target_id": "egfr", "smiles": d["smiles"]}, headers=H("a"))
        if r.status_code == 200 and r.json()["source"] == "precomputed":
            found = r.json()
            break
    assert found is not None and found["status"] == "done" and found["ligand_sdf"]
    assert client.get(f"/lead/api/poses/{found['job_id']}", headers=H("a")).status_code == 200
    assert client.get(f"/lead/api/poses/{found['job_id']}", headers=H("b")).status_code == 404
