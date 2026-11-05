"""/lead/api/adapt/jobs: availability (503 without torch / in mock mode), validation, per-user job isolation,
one active job per user; plus the SMARTS checker caps."""
from __future__ import annotations

import time

from conftest import H, make_app
from fastapi.testclient import TestClient

from leadstudio.adapt.jobs import JobManager
from leadstudio.server import routes

STEP = {"parent_smiles": "CCO", "edit_id": "x", "site": [0], "child_smiles": "CCN", "reward": 0.1}


def _live(factory, users, monkeypatch, runner):
    app = make_app(factory, users)
    app.state.lead.engine = object()  # source -> "live" without loading the engine
    monkeypatch.setattr(routes.importlib.util, "find_spec", lambda name: object())
    app.state.lead_adapt = JobManager(runner)
    return TestClient(app)


def test_adapt_unavailable_in_mock_mode(factory, users):
    c = TestClient(make_app(factory, users))
    r = c.post("/lead/api/adapt/jobs", json={"transitions": [STEP] * 5}, headers=H("a"))
    assert r.status_code == 503


def test_adapt_validation(factory, users, monkeypatch):
    c = _live(factory, users, monkeypatch, lambda *a, **k: {})
    assert c.post("/lead/api/adapt/jobs", json={}, headers=H("a")).status_code == 422
    both = {"transitions": [STEP] * 5, "manual_session_id": "00000000-0000-0000-0000-000000000000"}
    assert c.post("/lead/api/adapt/jobs", json=both, headers=H("a")).status_code == 422
    assert c.post("/lead/api/adapt/jobs", json={"transitions": [STEP]}, headers=H("a")).status_code == 422
    r = c.post("/lead/api/adapt/jobs", json={"manual_session_id": "00000000-0000-0000-0000-000000000000"},
               headers=H("a"))
    assert r.status_code == 404
    assert c.post("/lead/api/adapt/jobs", json={"transitions": [STEP] * 5, "x": 1}, headers=H("a")).status_code == 422


def test_adapt_jobs_are_owner_only_and_one_active(factory, users, monkeypatch):
    def runner(trans, owner, progress=None):
        time.sleep(0.5)
        return {"n_steps": len(trans)}

    c = _live(factory, users, monkeypatch, runner)
    r = c.post("/lead/api/adapt/jobs", json={"transitions": [STEP] * 5}, headers=H("a"))
    assert r.status_code == 202 and r.json()["source"] == "live"
    jid = r.json()["job_id"]
    assert c.get(f"/lead/api/adapt/jobs/{jid}", headers=H("b")).status_code == 404
    assert c.post("/lead/api/adapt/jobs", json={"transitions": [STEP] * 5}, headers=H("a")).status_code == 429
    for _ in range(40):
        j = c.get(f"/lead/api/adapt/jobs/{jid}", headers=H("a")).json()
        if j["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert j["status"] == "done" and j["result"] == {"n_steps": 5}


def test_adapt_requires_login(factory, users):
    c = TestClient(make_app(factory, users))
    assert c.post("/lead/api/adapt/jobs", json={}).status_code == 401
    assert c.get("/lead/api/adapt/jobs/abcdef").status_code == 401


def test_check_smarts_caps(factory, users):
    c = TestClient(make_app(factory, users))
    ok = c.post("/lead/profiles/check-smarts", json={"patterns": ["c1ccccc1", "[", "x" * 201]}, headers=H("a"))
    assert [x["ok"] for x in ok.json()["results"]] == [True, False, False]
    assert c.post("/lead/profiles/check-smarts", json={"patterns": ["C"] * 21}, headers=H("a")).status_code == 422
