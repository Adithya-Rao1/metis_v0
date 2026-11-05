"""One real (non-mock) run through the whole stack: RDKit scorer, registry, GA, SSE, saved profile. CPU only, a few
seconds. Also pins that the response says source "live" and that a profile changes ONLY its own run."""
from __future__ import annotations

import json

import pytest
from conftest import H, make_app
from fastapi.testclient import TestClient

from leadstudio.server.core import Settings

NITRO = "CCOC(=O)c1ccc(cc1)[N+](=O)[O-]"
BODY = {"seed_smiles": NITRO, "generations": 2, "population_size": 6, "rng_seed": 3}


@pytest.fixture
def live(factory, users):
    app = make_app(factory, users, Settings(mock_delay=0.0, replay_delay=0.0, runs_per_minute=1000, per_user_active=10))
    assert app.state.lead.engine is not None, "the real engine failed to build"
    return TestClient(app)


def run(c, who, **extra):
    r = c.post("/lead/api/runs", json={**BODY, **extra}, headers=H(who))
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "live"
    text = c.get(f"/lead/api/runs/{r.json()['run_id']}/events", headers=H(who)).text
    return [json.loads(ln[6:]) for ln in text.splitlines() if ln.startswith("data: ")]


def test_health_says_live(live):
    h = live.get("/lead/api/health", headers=H("a")).json()
    assert h["source"] == "live" and h["details"]["registry_ops"] > 0
    assert h["details"]["policy"]["kind"] == "uniform" and h["details"]["policy"]["checkpoint"] is None
    assert h["components"]["binding_stage"] is False   # PLAPT is not part of Metis
    v = live.post("/lead/api/molecules/validate", json={"smiles": NITRO}, headers=H("a")).json()
    assert v["ok"] and v["source"] == "live" and v["score"]["composite"] > 0


def test_live_run_streams_and_second_identical_run_is_a_replay(live):
    evs = run(live, "a")
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end" and "error" not in kinds
    assert evs[0]["payload"]["source"] == "live"
    assert any(e["type"] == "candidate" for e in evs)
    again = run(live, "a")
    assert again[0]["payload"]["source"] == "replay" and len(again) == len(evs)
    other = run(live, "b")
    assert other[0]["payload"]["source"] == "live"   # B never receives A's cached run


def test_binding_stage_degrades_to_503_without_plapt(live):
    r = live.post("/lead/api/runs", json={**BODY, "stage": "binding", "target_id": "egfr"}, headers=H("a"))
    assert r.status_code == 503 and "PLAPT" in r.json()["detail"]


def test_profile_changes_only_its_own_run(live):
    pid = live.post("/lead/profiles", json={"name": "avoid nitro", "objectives": {}, "avoid_smarts": ["[N+](=O)[O-]"],
                                            "edit_allowlist": []}, headers=H("a")).json()["id"]
    plain = run(live, "a")[0]["payload"]["seed_score"]
    prof = run(live, "a", profile_id=pid)[0]["payload"]["seed_score"]
    assert prof["composite"] < plain["composite"]
    assert any(a["family"] == "USER" for a in prof["alerts"])
    assert not any(a["family"] == "USER" for a in plain["alerts"])
    again = run(live, "a")[0]["payload"]["seed_score"]       # the shared engine was not mutated
    assert again["composite"] == plain["composite"]
    assert run(live, "b")[0]["payload"]["seed_score"]["composite"] == plain["composite"]
