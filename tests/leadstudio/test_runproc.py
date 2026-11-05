"""Process isolation of Lead Studio compute: every live run is a child process (killed on timeout, cancel, client
stop and shutdown), a crash in it leaves the API serving, children inherit no secrets, and the validate / preview /
SMARTS jobs run in a killable worker pool. Stand-in engines come from runproc_fakes.py (a child module)."""
from __future__ import annotations

import json
import os
import time
import uuid

import pytest
from conftest import H, make_app
from fastapi import HTTPException
from fastapi.testclient import TestClient

from leadstudio.schema import RunConfig
from leadstudio.server import procutil
from leadstudio.server.core import Rejected, RunManager, Settings
from leadstudio.server.heavy import HeavyFailed, HeavyPool

CFG = RunConfig(seed_smiles="CCO")
NITRO = "CCOC(=O)c1ccc(cc1)[N+](=O)[O-]"


_BASE = [0]


def extra_children() -> int:
    """Live child processes beyond those that existed when the test started (a module-scoped pool keeps one)."""
    return procutil.live_children() - _BASE[0]


@pytest.fixture(autouse=True)
def _baseline():
    _BASE[0] = procutil.live_children()


@pytest.fixture
def fake(monkeypatch):
    """Select the stand-in engine the child runs."""
    monkeypatch.syspath_prepend(os.path.dirname(__file__))

    def select(name: str) -> None:
        monkeypatch.setenv("STUDIO_TEST_FAKE", name)

    return select


@pytest.fixture
def managers():
    made: list[RunManager] = []

    def make(**kw) -> RunManager:
        mgr = RunManager(Settings(**{"orphan_grace": 0, "per_user_active": 50, "max_queue": 50, **kw}), object(), [], child_module="runproc_fakes")
        made.append(mgr)
        return mgr

    yield make
    for m in made:
        m.shutdown()
    assert extra_children() == 0, "a child process outlived its manager"


def wait(pred, secs=15.0):
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def wait_child(run):
    assert wait(lambda: run.child is not None), "the run's process never started"
    return run.child


# --------------------------------------------------------------------------------------------- a normal run
def test_a_run_executes_in_another_process_and_its_events_come_back(fake, managers):
    fake("Quick")
    mgr = managers(run_timeout=30)
    run = mgr.submit(uuid.uuid4(), CFG, "q1")
    assert wait(lambda: run.finished) and run.status == "done"
    assert [e["type"] for e in run.events] == ["run_start", "run_end"] and [e["seq"] for e in run.events] == [0, 1]


def test_the_child_gets_no_secrets_and_a_different_pid(fake, managers, monkeypatch):
    for k, v in {"METIS_SECRET_KEY": "s3cret", "METIS_DATABASE_URL": "postgresql://u:pw@h/db", "AWS_SECRET_ACCESS_KEY": "x",
                 "STUDIO_SESSION_SECRET": "y", "OPENAI_API_KEY": "z"}.items():
        monkeypatch.setenv(k, v)
    fake("Env")
    mgr = managers(run_timeout=30)
    run = mgr.submit(uuid.uuid4(), CFG, "e1")
    assert wait(lambda: run.finished) and run.status == "done"
    payload = run.events[0]["payload"]
    assert payload["pid"] != os.getpid()
    leaked = [k for k in payload["env"] if k.startswith("METIS_") or "SECRET" in k or "KEY" in k or "PASSWORD" in k or "DATABASE" in k]
    assert leaked == [], leaked
    assert set(payload["env"]) <= {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
                                   "STUDIO_TEST_FAKE", "SYSTEMROOT", "__CF_USER_TEXT_ENCODING", "LC_CTYPE", "XDG_CACHE_HOME",
                                   "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "HF_HOME", "HF_HUB_OFFLINE",
                                   "TRANSFORMERS_OFFLINE", "TORCH_HOME", "RDBASE"}  # RDBASE is set by RDKit itself


# ------------------------------------------------------------------------------------ kill on timeout / cancel
def test_a_timed_out_run_has_its_process_killed(fake, managers):
    fake("Hung")
    mgr = managers(run_timeout=0.8, max_concurrent=1)
    run = mgr.submit(uuid.uuid4(), CFG, "t1")
    child = wait_child(run)
    pid = child.pid
    assert wait(lambda: run.finished) and run.status == "error"
    assert "time limit" in run.events[-1]["payload"]["message"]
    assert wait(lambda: pid_gone(pid)), "the timed-out run's process is still alive"
    nxt = mgr.submit(uuid.uuid4(), CFG, "t2")              # the slot is free again: no leaked thread, no leaked process
    assert wait(lambda: nxt.status == "running")


def test_cancel_kills_the_child_at_once(fake, managers):
    fake("Hung")
    mgr = managers(run_timeout=60)
    run = mgr.submit(uuid.uuid4(), CFG, "c1")
    child = wait_child(run)
    pid = child.pid
    t = time.monotonic()
    assert mgr.cancel(run) is True
    assert wait(lambda: pid_gone(pid), 5) and time.monotonic() - t < 3
    assert run.status == "cancelled"


def test_a_client_that_left_cancels_the_run_and_kills_the_child(fake, managers):
    fake("Hung")
    mgr = managers(run_timeout=60)
    mgr.s.orphan_grace = 0.3                               # nobody ever opens the event stream
    run = mgr.submit(uuid.uuid4(), CFG, "o1")
    pid = wait_child(run).pid
    assert wait(lambda: run.status == "cancelled", 5)
    assert wait(lambda: pid_gone(pid), 5)


def test_a_cancel_while_queued_never_starts_a_process(fake, managers):
    fake("Hung")
    mgr = managers(run_timeout=60, max_concurrent=1)
    first = mgr.submit(uuid.uuid4(), CFG, "a")
    wait_child(first)
    second = mgr.submit(uuid.uuid4(), CFG, "b")
    assert second.status == "queued" and mgr.cancel(second)
    mgr.cancel(first)
    assert wait(lambda: extra_children() == 0, 5)
    assert second.child is None


def test_shutdown_leaves_no_orphan_processes(fake, managers):
    fake("Hung")
    mgr = managers(run_timeout=60, max_concurrent=2)
    runs = [mgr.submit(uuid.uuid4(), CFG, f"s{i}") for i in range(2)]
    pids = [wait_child(r).pid for r in runs]
    mgr.shutdown()
    assert all(pid_gone(p) for p in pids)
    assert extra_children() == 0


def test_forgetting_a_user_kills_their_runs(fake, managers):
    fake("Hung")
    mgr = managers(run_timeout=60)
    uid = uuid.uuid4()
    run = mgr.submit(uid, CFG, "f1")
    pid = wait_child(run).pid
    assert mgr.forget_user(uid) == 1
    assert wait(lambda: pid_gone(pid), 5)


# --------------------------------------------------------------------------------------------- crashes
def test_a_crashing_child_fails_the_run_with_a_plain_message_and_the_manager_keeps_working(fake, managers):
    fake("Crash")
    mgr = managers(run_timeout=30, max_concurrent=1)
    run = mgr.submit(uuid.uuid4(), CFG, "x1")
    assert wait(lambda: run.finished) and run.status == "error"
    msg = run.events[-1]["payload"]["message"]
    assert "stopped unexpectedly" in msg and "139" not in msg and "Traceback" not in msg
    fake("Quick")                                          # the next run (a new child) is fine
    ok = mgr.submit(uuid.uuid4(), CFG, "x2")
    assert wait(lambda: ok.finished) and ok.status == "done"


def test_a_child_that_floods_events_is_stopped(fake, managers):
    fake("Chatty")
    mgr = managers(run_timeout=30, max_run_events=50)
    run = mgr.submit(uuid.uuid4(), CFG, "fl")
    assert wait(lambda: run.finished) and run.status == "error"
    assert len(run.events) <= 52 and "too many" in run.events[-1]["payload"]["message"]
    assert wait(lambda: extra_children() == 0, 5)


def test_a_missing_child_module_is_a_failed_run_not_an_exception(managers):
    mgr = RunManager(Settings(run_timeout=10, orphan_grace=0), object(), [], child_module="no_such_module_anywhere")
    try:
        run = mgr.submit(uuid.uuid4(), CFG, "m1")
        assert wait(lambda: run.finished) and run.status == "error"
    finally:
        mgr.shutdown()


# ---------------------------------------------------------------------------------------- caps unchanged
def test_caps_are_unchanged_with_child_processes(fake, managers):
    fake("Hung")
    mgr = managers(run_timeout=60, max_concurrent=1, max_queue=1, per_user_active=1)
    u1, u2, u3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    mgr.submit(u1, CFG, "a")
    with pytest.raises(Rejected) as e1:
        mgr.submit(u1, CFG, "b")
    assert e1.value.status_code == 429
    mgr.submit(u2, CFG, "c")
    with pytest.raises(Rejected) as e2:
        mgr.submit(u3, CFG, "d")
    assert e2.value.status_code == 429 and e2.value.headers["Retry-After"].isdigit()
    assert len(mgr.pool._threads) <= 1                       # at most max_concurrent supervisors, however many were refused


def test_many_stuck_runs_in_a_row_never_exhaust_the_service(fake, managers):
    fake("Hung")
    mgr = managers(run_timeout=0.4, max_concurrent=1)
    for i in range(6):                                      # the old design refused new work after 4 stuck threads
        run = mgr.submit(uuid.uuid4(), CFG, f"k{i}")
        assert wait(lambda: run.finished), i
    assert wait(lambda: extra_children() == 0, 5)


# ------------------------------------------------------------------------- the real engine, end to end
@pytest.fixture
def live(factory, users, monkeypatch):
    monkeypatch.delenv("STUDIO_FORCE_MOCK", raising=False)
    app = make_app(factory, users, Settings(mock_delay=0.0, replay_delay=0.0, runs_per_minute=1000, per_user_active=10, heartbeat=0.2))
    assert app.state.lead.engine is not None
    yield TestClient(app)
    app.state.lead.shutdown()


def test_a_real_run_survives_a_killed_child_and_the_api_keeps_serving(live):
    r = live.post("/lead/api/runs", json={"seed_smiles": NITRO, "generations": 15, "population_size": 40, "rng_seed": 1}, headers=H("a"))
    assert r.status_code == 200 and r.json()["source"] == "live"
    run = live.app.state.lead.mgr.runs[r.json()["run_id"]]
    child = wait_child(run)
    os.kill(child.pid, 9)                                   # a native crash, from outside
    text = live.get(f"/lead/api/runs/{r.json()['run_id']}/events", headers=H("a")).text
    evs = [json.loads(ln[6:]) for ln in text.splitlines() if ln.startswith("data: ")]
    assert evs[-1]["type"] == "error" and "stopped unexpectedly" in evs[-1]["payload"]["message"]
    assert live.get("/lead/api/health", headers=H("a")).status_code == 200
    ok = live.post("/lead/api/molecules/validate", json={"smiles": NITRO}, headers=H("a")).json()
    assert ok["ok"]


def test_cross_user_isolation_holds_with_child_processes(live):
    ra = live.post("/lead/api/runs", json={"seed_smiles": NITRO, "generations": 2, "population_size": 6}, headers=H("a")).json()["run_id"]
    assert live.get(f"/lead/api/runs/{ra}/events", headers=H("b")).status_code == 404
    assert live.delete(f"/lead/api/runs/{ra}", headers=H("b")).status_code == 404
    text = live.get(f"/lead/api/runs/{ra}/events", headers=H("a")).text
    assert '"run_end"' in text


# ------------------------------------------------------------------------------------------ heavy pool
@pytest.fixture
def pool():
    made: list[HeavyPool] = []

    def make(**kw) -> HeavyPool:
        p = HeavyPool(**kw)
        made.append(p)
        return p

    yield make
    for p in made:
        p.close()
    assert extra_children() == 0


@pytest.fixture(scope="module")
def real_pool():
    p = HeavyPool(workers=1, timeout=10.0)
    yield p
    p.close()


def test_validation_runs_in_a_worker_process(real_pool):
    p = real_pool
    res = p.call("validate", {"smiles": NITRO, "score": True})
    assert res["ok"] and res["score"]["composite"] > 0 and res["molblock"]
    assert p.call("parse", {"items": [{"smiles": "xx"}]})[0]["ok"] is False
    assert p.call("smarts", {"patterns": ["C", "((("]})[1]["ok"] is False
    assert p.call("applicable", {"smiles": NITRO, "locked": ""})
    with pytest.raises(HTTPException) as e:                  # the operation's own HTTP error comes through unchanged
        p.call("applicable", {"smiles": "xx", "locked": ""})
    assert e.value.status_code == 422


def test_a_failing_job_gives_a_plain_error_and_the_next_job_works(real_pool):
    with pytest.raises(HeavyFailed) as e:
        real_pool.call("no_such_operation", {})              # the worker raises: it is replaced, the caller gets a 422
    assert not e.value.timed_out and e.value.status_code == 422
    assert real_pool.call("parse", {"items": [{"smiles": "CCO"}]})[0]["ok"]


def test_a_worker_that_never_answers_is_killed_on_timeout(pool, monkeypatch):
    monkeypatch.syspath_prepend(os.path.dirname(__file__))
    p = pool(workers=1, timeout=1.0, module="runproc_heavy_fake")
    with pytest.raises(HeavyFailed) as e:
        p.call("anything", {})
    assert e.value.timed_out
    assert p._count == 0 and extra_children() == 0   # the hung worker is gone


def test_a_crashing_worker_is_replaced(pool, monkeypatch):
    monkeypatch.syspath_prepend(os.path.dirname(__file__))
    monkeypatch.setenv("STUDIO_TEST_FAKE", "crash")
    p = pool(workers=1, timeout=5.0, module="runproc_heavy_fake")
    with pytest.raises(HeavyFailed) as e:
        p.call("anything", {})
    assert not e.value.timed_out
    monkeypatch.setenv("STUDIO_TEST_FAKE", "echo")
    assert p.call("anything", {"a": 1})["args"] == {"a": 1}          # a fresh worker answers


def test_workers_are_recycled_after_n_jobs(pool, monkeypatch):
    monkeypatch.syspath_prepend(os.path.dirname(__file__))
    monkeypatch.setenv("STUDIO_TEST_FAKE", "echo")
    p = pool(workers=1, recycle_after=3, module="runproc_heavy_fake")
    pids = [p.call("x", {})["pid"] for _ in range(7)]
    assert pids[0] == pids[1] == pids[2] and pids[3] == pids[4] == pids[5] and pids[0] != pids[3] != pids[6]


def test_the_pool_refuses_work_beyond_its_bound_at_once(pool, monkeypatch):
    monkeypatch.syspath_prepend(os.path.dirname(__file__))
    monkeypatch.setenv("STUDIO_TEST_FAKE", "slow")
    p = pool(workers=1, max_waiting=0, timeout=5.0, module="runproc_heavy_fake")
    import threading

    t = threading.Thread(target=lambda: p.call("anything", {}))
    t.start()
    assert wait(lambda: p._count == 1)
    with pytest.raises(Rejected) as e:
        p.call("anything", {})
    assert e.value.status_code == 429
    t.join(10)


def test_a_hostile_smarts_cannot_hang_the_api_through_the_routes(live):
    r = live.post("/lead/profiles/check-smarts", json={"patterns": ["[N+](=O)[O-]", "((("]}, headers=H("a")).json()["results"]
    assert [x["ok"] for x in r] == [True, False]


# ---------------------------------------------------------------------------------------------------- lifespan
def test_a_clean_app_stop_shuts_the_lead_state_down_and_kills_the_workers(factory, users):
    """Leaving the app's lifespan (a clean stop) calls LeadState.shutdown(): no heavy worker outlives the app."""
    app = make_app(factory, users)
    calls: list[str] = []
    st = app.state.lead
    real_shutdown = st.shutdown
    st.shutdown = lambda: (calls.append("shutdown"), real_shutdown())  # type: ignore[method-assign]  # test spy
    with TestClient(app) as c:
        r = c.post("/lead/api/molecules/validate", json={"smiles": NITRO}, headers=H("a"))
        assert r.status_code == 200
        assert extra_children() >= 1  # the heavy worker that parsed the molecule
    assert calls == ["shutdown"]
    assert extra_children() == 0


def test_a_clean_stop_without_lead_state_is_a_no_op():
    from backend.main import create_app

    with TestClient(create_app()) as c:
        assert c.get("/health").status_code == 200
