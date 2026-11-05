"""Lead Studio API behaviour in mock mode (no GPU, no network): health, validation, runs + SSE, targets, data, poses,
manual mode, caps and rate limits. Cross-user isolation lives in test_isolation.py."""
from __future__ import annotations

import json
import time
import uuid

from conftest import H, make_app
from fastapi.testclient import TestClient

from leadstudio.server.core import Settings

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
NITRO = "CCOC(=O)c1ccc(cc1)[N+](=O)[O-]"


def events(client, run_id, who="a"):
    r = client.get(f"/lead/api/runs/{run_id}/events", headers=H(who))
    assert r.status_code == 200, r.text
    out = []
    for block in r.text.split("\n\n"):
        lines = dict(ln.split(": ", 1) for ln in block.splitlines() if ": " in ln and not ln.startswith(":"))
        if "data" in lines:
            out.append(json.loads(lines["data"]))
    return out


# ------------------------------------------------------------------------------------------------- basics
def test_health_says_mock_and_lists_limits(client):
    h = client.get("/lead/api/health", headers=H("a")).json()
    assert h["ok"] and h["source"] == "mock" and h["engine_source"] == "mock" and h["force_mock"] is True
    assert h["limits"]["runs_per_user"] == 2 and h["limits"]["max_heavy_atoms"] == 60
    assert "dynamicbind" not in json.dumps(h).lower()  # the service URL is never exposed


def test_presets_and_targets(client):
    assert isinstance(client.get("/lead/api/presets", headers=H("a")).json(), list)
    t = client.get("/lead/api/targets", headers=H("a")).json()
    ids = {x["id"] for x in t}
    assert {"egfr", "abl1", "braf", "fxa", "hiv1_protease", "alpha_synuclein"} <= ids
    assert client.get("/lead/api/targets/egfr", headers=H("a")).json()["pdb_id"] == "1M17"
    assert client.get("/lead/api/targets/nope", headers=H("a")).status_code == 404
    assert client.get("/lead/api/targets/..%2f..%2fetc", headers=H("a")).status_code in (404, 422)


def test_data_files_are_whitelisted(client):
    ok = client.get("/lead/api/data/structures/egfr.pdb", headers=H("a"))
    assert ok.status_code == 200 and ok.text.startswith(("HEADER", "ATOM", "REMARK", "CRYST1", "TITLE"))
    for bad in ("../targets/egfr.json", "..%2Ftargets%2Fegfr.json", "structures/../../schema.py", ".hidden",
                "structures/egfr.py", "%00", "structures//egfr.pdb"):
        assert client.get(f"/lead/api/data/{bad}", headers=H("a")).status_code == 404, bad


def test_validate_molecule(client):
    r = client.post("/lead/api/molecules/validate", json={"smiles": ASPIRIN}, headers=H("a")).json()
    assert r["ok"] and r["heavy_atoms"] == 13 and r["molblock"] and r["source"] == "mock"
    bad = client.post("/lead/api/molecules/validate", json={"smiles": "not-a-smiles"}, headers=H("a")).json()
    assert bad["ok"] is False and bad["error"]
    big = client.post("/lead/api/molecules/validate", json={"smiles": "C" * 61}, headers=H("a")).json()
    assert big["ok"] is False and "too large" in big["error"]
    nul = client.post("/lead/api/molecules/validate", json={"smiles": "CC\u0000O"}, headers=H("a")).json()
    assert nul["ok"] is False
    assert client.post("/lead/api/molecules/validate", json={"smiles": "C" * 2001}, headers=H("a")).status_code == 422
    assert client.post("/lead/api/molecules/validate", json={"molfile": "x" * 150_001}, headers=H("a")).status_code == 422


# ---------------------------------------------------------------------------------------------------- runs
def test_run_streams_mock_events_and_labels_source(client):
    r = client.post("/lead/api/runs", json={"seed_smiles": NITRO}, headers=H("a"))
    assert r.status_code == 200 and r.json()["source"] == "mock"
    evs = events(client, r.json()["run_id"])
    types = [e["type"] for e in evs]
    assert types[0] == "run_start" and types[-1] == "run_end" and "candidate" in types
    assert evs[0]["payload"]["source"] == "mock"
    assert [e["seq"] for e in evs] == list(range(len(evs)))


def test_replay_of_identical_config_is_served_from_cache_only_for_live_runs(client):
    # mock runs are never cached (only live runs are), so a second identical run is again a mock run
    a = client.post("/lead/api/runs", json={"seed_smiles": NITRO}, headers=H("a")).json()
    events(client, a["run_id"])
    b = client.post("/lead/api/runs", json={"seed_smiles": NITRO}, headers=H("a")).json()
    assert events(client, b["run_id"])[0]["payload"]["source"] == "mock"


def test_run_validation_and_server_caps(factory, users):
    app = make_app(factory, users, Settings(force_mock=True, mock_delay=0.0, replay_delay=0.0, runs_per_minute=1000,
                                            per_user_active=1000))
    client = TestClient(app)
    post = lambda body: client.post("/lead/api/runs", json=body, headers=H("a"))  # noqa: E731
    assert post({"seed_smiles": "nope"}).status_code == 422
    assert post({"seed_smiles": "C" * 2001}).status_code == 422
    assert post({"seed_smiles": "C" * 61}).status_code == 422                      # > 60 heavy atoms
    assert post({"seed_smiles": NITRO, "locked_atoms": [999]}).status_code == 422
    assert post({"seed_smiles": NITRO, "locked_atoms": list(range(121))}).status_code == 422
    assert post({"seed_smiles": NITRO, "generations": 0}).status_code == 422
    assert post({"seed_smiles": NITRO, "generations": 10**9}).status_code == 422
    assert post({"seed_smiles": NITRO, "objectives": {"__class__": {}}}).status_code == 422
    assert post({"seed_smiles": NITRO, "objectives": {"qed": {"weight": 99}}}).status_code == 422
    assert post({"seed_smiles": NITRO, "objectives": {f"o{i}": {} for i in range(21)}}).status_code == 422
    assert post({"seed_smiles": NITRO, "adapter_id": "x"}).status_code == 422
    assert post({"seed_smiles": NITRO, "stage": "manual"}).status_code == 422
    assert post({"seed_smiles": NITRO, "target_id": "nope"}).status_code == 422
    assert post({"seed_smiles": NITRO, "stage": "binding", "target_id": "egfr"}).status_code == 503  # mock mode
    # absurd-but-legal sizes are clamped to the server limits, not rejected
    ok = post({"seed_smiles": NITRO, "generations": 9999, "population_size": 9999})
    assert ok.status_code == 200
    run = app.state.lead.mgr.runs[ok.json()["run_id"]]
    assert run.config.generations == 15 and run.config.population_size == 40 and run.config.seed_molblock is None


def test_nan_and_infinity_are_rejected_by_the_app(client):
    r = client.post("/lead/api/runs", content='{"seed_smiles":"CCO","policy_guidance":NaN}',
                    headers={**H("a"), "content-type": "application/json"})
    assert r.status_code == 422


def test_body_over_cap_is_413(client):
    r = client.post("/lead/api/molecules/validate", content=b'{"smiles":"' + b"C" * 3_000_000 + b'"}',
                    headers={**H("a"), "content-type": "application/json"})
    assert r.status_code == 413


def test_cancel_run_and_unknown_run(client):
    run_id = client.post("/lead/api/runs", json={"seed_smiles": NITRO}, headers=H("a")).json()["run_id"]
    assert client.delete(f"/lead/api/runs/{run_id}", headers=H("a")).status_code == 200
    assert client.get("/lead/api/runs/nope/events", headers=H("a")).status_code == 404
    assert client.get("/lead/api/runs/bad%20id/events", headers=H("a")).status_code == 422


def test_per_user_active_run_cap(factory, users):
    app = make_app(factory, users, Settings(force_mock=True, mock_delay=0.5, replay_delay=0.0, per_user_active=2,
                                            runs_per_minute=1000))
    c = TestClient(app)
    ids = [c.post("/lead/api/runs", json={"seed_smiles": NITRO, "rng_seed": i}, headers=H("a")) for i in range(3)]
    assert [r.status_code for r in ids] == [200, 200, 429]
    assert "Retry-After" in ids[2].headers
    other = c.post("/lead/api/runs", json={"seed_smiles": NITRO}, headers=H("b"))
    assert other.status_code == 200  # another user is not blocked by a's runs (process cap is higher)


def test_process_wide_run_cap(factory, users):
    app = make_app(factory, users, Settings(force_mock=True, mock_delay=0.5, replay_delay=0.0, max_concurrent=1,
                                            max_queue=1, per_user_active=5, runs_per_minute=1000))
    c = TestClient(app)
    codes = [c.post("/lead/api/runs", json={"seed_smiles": NITRO, "rng_seed": i}, headers=H(u)).status_code
             for i, u in enumerate("aabb")]
    assert codes == [200, 200, 429, 429]


def test_run_rate_limit_is_per_user_and_database_backed(factory, users):
    app = make_app(factory, users, Settings(force_mock=True, mock_delay=0.0, replay_delay=0.0, runs_per_minute=2,
                                            per_user_active=50))
    c = TestClient(app)
    codes = [c.post("/lead/api/runs", json={"seed_smiles": NITRO, "rng_seed": i}, headers=H("a")).status_code
             for i in range(4)]
    assert codes[:2] == [200, 200] and codes[2:] == [429, 429]
    r = c.post("/lead/api/runs", json={"seed_smiles": NITRO}, headers=H("a"))
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert c.post("/lead/api/runs", json={"seed_smiles": NITRO}, headers=H("b")).status_code == 200


def test_validate_rate_limit(factory, users):
    app = make_app(factory, users, Settings(force_mock=True, validates_per_minute=2))
    c = TestClient(app)
    codes = [c.post("/lead/api/molecules/validate", json={"smiles": "CCO"}, headers=H("a")).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_stream_cap_per_user(factory, users):
    app = make_app(factory, users, Settings(force_mock=True, per_user_streams=1))
    st = app.state.lead
    assert st.mgr.open_stream(users["a"].id) and not st.mgr.open_stream(users["a"].id)
    assert st.mgr.open_stream(users["b"].id)
    st.mgr.close_stream(users["a"].id)
    assert st.mgr.open_stream(users["a"].id)


def test_runs_do_not_block_the_event_loop(factory, users):
    """The mock replay sleeps 0.3 s per event on a worker thread; health must answer immediately meanwhile."""
    app = make_app(factory, users, Settings(force_mock=True, mock_delay=0.3, replay_delay=0.0))
    c = TestClient(app)
    c.post("/lead/api/runs", json={"seed_smiles": NITRO}, headers=H("a"))
    t = time.perf_counter()
    assert c.get("/lead/api/health", headers=H("a")).status_code == 200
    assert time.perf_counter() - t < 0.25


def test_run_timeout_ends_the_stream_with_an_error(factory, users):
    app = make_app(factory, users, Settings(force_mock=True, mock_delay=0.2, replay_delay=0.0, run_timeout=0.3))
    c = TestClient(app)
    rid = c.post("/lead/api/runs", json={"seed_smiles": NITRO}, headers=H("a")).json()["run_id"]
    evs = events(c, rid)
    assert evs[-1]["type"] == "error" and "time limit" in evs[-1]["payload"]["message"]


# --------------------------------------------------------------------------------------------- manual mode
def test_manual_applicable_preview_and_session(client):
    app_ = client.get("/lead/api/manual/applicable", params={"smiles": NITRO}, headers=H("a"))
    assert app_.status_code == 200 and app_.json()
    op = app_.json()[0]
    pv = client.post("/lead/api/manual/preview", json={"smiles": NITRO, "edit_id": op["edit_id"], "site": op["site"]},
                     headers=H("a"))
    assert pv.status_code == 200, pv.text
    body = pv.json()
    if body["ok"]:
        assert body["child_smiles"] and body["molblock"] and body["score"]
    unknown = client.post("/lead/api/manual/preview", json={"smiles": NITRO, "edit_id": "nope", "site": [0]}, headers=H("a"))
    assert unknown.json()["ok"] is False
    oob = client.post("/lead/api/manual/preview", json={"smiles": NITRO, "edit_id": op["edit_id"], "site": [999]}, headers=H("a"))
    assert oob.json()["ok"] is False
    assert client.get("/lead/api/manual/applicable", params={"smiles": "xx"}, headers=H("a")).status_code == 422
    assert client.get("/lead/api/manual/applicable", params={"smiles": NITRO, "locked": "a,b"}, headers=H("a")).status_code == 422
    assert client.post("/lead/api/manual/preview", json={"smiles": NITRO, "edit_id": "e", "site": list(range(65))},
                       headers=H("a")).status_code == 422

    s = client.post("/lead/api/manual/sessions", json={"seed_smiles": NITRO}, headers=H("a")).json()
    step = {"parent_smiles": NITRO, "child_smiles": ASPIRIN, "edit_id": op["edit_id"], "site": op["site"]}
    s2 = client.post(f"/lead/api/manual/sessions/{s['session_id']}/steps", json=step, headers=H("a")).json()
    assert len(s2["steps"]) == 1 and s2["steps"][0]["reward_delta"] is not None
    exp = client.get(f"/lead/api/manual/sessions/{s['session_id']}/export", headers=H("a"))
    assert exp.headers["content-type"].startswith("application/x-ndjson")
    line = json.loads(exp.text.splitlines()[0])
    assert line["chosen_by"] == "user" and line["child_smiles"] == ASPIRIN and "reward" in line


def test_manual_step_validation_and_caps(factory, users):
    app = make_app(factory, users, Settings(force_mock=True, max_live_steps=2, max_live_sessions_per_user=2))
    c = TestClient(app)
    sid = c.post("/lead/api/manual/sessions", json={"seed_smiles": NITRO}, headers=H("a")).json()["session_id"]
    step = {"parent_smiles": NITRO, "child_smiles": ASPIRIN, "edit_id": "x", "site": [0], "reward_delta": 0.1}
    url = f"/lead/api/manual/sessions/{sid}/steps"
    assert c.post(url, json={**step, "child_smiles": "bad"}, headers=H("a")).status_code == 422
    assert c.post(url, json={**step, "edit_id": "bad id!"}, headers=H("a")).status_code == 422
    assert c.post(url, json={**step, "reward_delta": 1e12}, headers=H("a")).status_code == 422
    assert [c.post(url, json=step, headers=H("a")).status_code for _ in range(3)] == [200, 200, 429]
    assert c.post("/lead/api/manual/sessions", json={"seed_smiles": "bad"}, headers=H("a")).status_code == 422
    assert c.post("/lead/api/manual/sessions", json={"seed_smiles": NITRO, "extra": 1}, headers=H("a")).status_code == 422
    # per-user session cap evicts that user's own oldest session
    for _ in range(2):
        c.post("/lead/api/manual/sessions", json={"seed_smiles": NITRO}, headers=H("a"))
    assert c.get(f"/lead/api/manual/sessions/{sid}/export", headers=H("a")).status_code == 404


# ------------------------------------------------------------------------------------------------- library
def mock_events(client):
    rid = client.post("/lead/api/runs", json={"seed_smiles": NITRO}, headers=H("a")).json()["run_id"]
    return events(client, rid)


def test_library_run_roundtrip_and_validation(client):
    evs = mock_events(client)
    r = client.post("/lead/library/runs", json={"events": evs}, headers=H("a"))
    assert r.status_code == 201, r.text
    saved = r.json()
    assert uuid.UUID(saved["id"]) and saved["seed_smiles"] and saved["stage"] == "pharmacology"
    lst = client.get("/lead/library/runs", headers=H("a")).json()["runs"]
    assert [x["id"] for x in lst] == [saved["id"]]
    got = client.get(f"/lead/library/runs/{saved['id']}", headers=H("a")).json()
    assert len(got["events"]) == len(evs) and got["events"][0]["type"] == "run_start"
    assert client.get("/lead/library/runs/not-a-uuid", headers=H("a")).status_code == 422
    assert client.get(f"/lead/library/runs/{uuid.uuid4()}", headers=H("a")).status_code == 404
    assert client.post("/lead/library/runs", json={"events": []}, headers=H("a")).status_code == 422
    assert client.post("/lead/library/runs", json={"events": [{"type": "run_start", "run_id": "r", "seq": 0, "payload": {}}]},
                       headers=H("a")).status_code == 422
    assert client.post("/lead/library/runs", json={"events": evs, "stage": "manual"}, headers=H("a")).status_code == 422
    assert client.post("/lead/library/runs", json={"events": evs, "junk": 1}, headers=H("a")).status_code == 422
    assert client.delete(f"/lead/library/runs/{saved['id']}", headers=H("a")).status_code == 200
    assert client.delete(f"/lead/library/runs/{saved['id']}", headers=H("a")).status_code == 404
    assert client.get("/lead/library/runs", headers=H("a")).json()["runs"] == []


def test_library_run_size_cap(client, monkeypatch):
    import leadstudio.server.routes as routes
    evs = mock_events(client)
    monkeypatch.setattr(routes, "MAX_SAVED_PAYLOAD", 1000)
    assert client.post("/lead/library/runs", json={"events": evs}, headers=H("a")).status_code == 413


def test_library_run_count_cap(client, monkeypatch):
    from leadstudio.server import store
    monkeypatch.setattr(store, "MAX_RUNS", 2)
    evs = mock_events(client)
    codes = [client.post("/lead/library/runs", json={"events": evs}, headers=H("a")).status_code for _ in range(3)]
    assert codes == [201, 201, 409]
    assert client.post("/lead/library/runs", json={"events": evs}, headers=H("b")).status_code == 201


def test_library_manual_roundtrip(client):
    step = {"parent_smiles": NITRO, "child_smiles": ASPIRIN, "edit_id": "x", "site": [1, 2], "reward": 0.25, "t": 1.5}
    jsonl = "\n".join(json.dumps(step) for _ in range(3)) + "\n"
    r = client.post("/lead/library/manual", json={"jsonl": jsonl, "target_id": "egfr"}, headers=H("a"))
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["n_steps"] == 3 and s["target_id"] == "egfr"
    got = client.get(f"/lead/library/manual/{s['id']}", headers=H("a")).json()
    assert [x["reward_delta"] for x in got["steps"]] == [0.25] * 3 and got["steps"][0]["site"] == [1, 2]
    assert [x["id"] for x in client.get("/lead/library/manual", headers=H("a")).json()["sessions"]] == [s["id"]]
    for bad in ({"jsonl": "{not json}"}, {"jsonl": ""}, {"steps": []}, {"steps": [{**step, "child_smiles": "zz"}]},
                {"steps": [{**step, "edit_id": ""}]}, {"steps": [{**step, "site": list(range(65))}]},
                {"steps": [step], "target_id": "bad id"}, {"steps": [step] * 501}):
        assert client.post("/lead/library/manual", json=bad, headers=H("a")).status_code == 422, bad
    assert client.delete(f"/lead/library/manual/{s['id']}", headers=H("a")).status_code == 200
    assert client.get(f"/lead/library/manual/{s['id']}", headers=H("a")).status_code == 404


def test_library_manual_count_cap(client, monkeypatch):
    from leadstudio.server import store
    monkeypatch.setattr(store, "MAX_MANUAL", 1)
    body = {"steps": [{"parent_smiles": NITRO, "child_smiles": ASPIRIN, "edit_id": "x", "site": [0]}]}
    assert [client.post("/lead/library/manual", json=body, headers=H("a")).status_code for _ in range(2)] == [201, 409]


# ------------------------------------------------------------------------------------------------ profiles
PROFILE = {"name": "No nitro", "objectives": {"qed": {"weight": 3, "direction": "maximize", "low": 0.3, "high": 0.9}},
           "avoid_smarts": ["[N+](=O)[O-]"], "edit_allowlist": []}


def test_profile_crud_and_validation(client):
    c = client.post("/lead/profiles", json=PROFILE, headers=H("a"))
    assert c.status_code == 201, c.text
    pid = c.json()["id"]
    assert client.get(f"/lead/profiles/{pid}", headers=H("a")).json()["avoid_smarts"] == ["[N+](=O)[O-]"]
    assert [p["id"] for p in client.get("/lead/profiles", headers=H("a")).json()["profiles"]] == [pid]
    up = client.put(f"/lead/profiles/{pid}", json={**PROFILE, "name": "Renamed"}, headers=H("a"))
    assert up.status_code == 200 and up.json()["name"] == "Renamed"
    for bad in ({**PROFILE, "name": " "}, {**PROFILE, "avoid_smarts": ["(((" ]}, {**PROFILE, "avoid_smarts": ["C"] * 21},
                {**PROFILE, "objectives": {"qed": {"weight": 11}}}, {**PROFILE, "edit_allowlist": ["no_such_edit"]},
                {**PROFILE, "name": "x" * 81}):
        assert client.post("/lead/profiles", json=bad, headers=H("a")).status_code == 422, bad
    chk = client.post("/lead/profiles/check-smarts", json={"patterns": ["c1ccccc1", "(((", ""]}, headers=H("a"))
    assert [r["ok"] for r in chk.json()["results"]] == [True, False, False]
    assert client.delete(f"/lead/profiles/{pid}", headers=H("a")).status_code == 200
    assert client.get(f"/lead/profiles/{pid}", headers=H("a")).status_code == 404


def test_profile_count_cap(client, monkeypatch):
    from leadstudio.server import store
    monkeypatch.setattr(store, "MAX_PROFILES", 2)
    codes = [client.post("/lead/profiles", json={**PROFILE, "name": f"p{i}"}, headers=H("a")).status_code for i in range(3)]
    assert codes == [201, 201, 409]


def test_run_with_profile_id_must_be_own_and_pharmacology(client):
    pid = client.post("/lead/profiles", json=PROFILE, headers=H("a")).json()["id"]
    assert client.post("/lead/api/runs", json={"seed_smiles": NITRO, "profile_id": pid}, headers=H("a")).status_code == 200
    assert client.post("/lead/api/runs", json={"seed_smiles": NITRO, "profile_id": pid}, headers=H("b")).status_code == 404
    assert client.post("/lead/api/runs", json={"seed_smiles": NITRO, "profile_id": "zzz"}, headers=H("a")).status_code == 422
    assert client.post("/lead/api/runs", json={"seed_smiles": NITRO, "profile_id": str(uuid.uuid4())}, headers=H("a")).status_code == 404
