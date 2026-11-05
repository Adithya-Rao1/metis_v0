"""Wave 4: SSE streams and runs are released when the client goes away; terms apply to every Lead Studio route; no
structure ever reaches a log call."""
from __future__ import annotations

import ast
import pathlib
import re
import time

import pytest
from conftest import H, make_app
from fastapi.testclient import TestClient

from leadstudio.schema import RunConfig
from leadstudio.server.core import Settings

NITRO = "CCOC(=O)c1ccc(cc1)[N+](=O)[O-]"
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _state(factory, users, **kw):
    s = Settings(force_mock=True, mock_delay=0.2, replay_delay=0.0, runs_per_minute=1000, per_user_active=10, **kw)
    app = make_app(factory, users, s)
    return app, app.state.lead


def _wait(pred, secs=5.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_a_run_nobody_listens_to_is_cancelled_after_the_grace_period(factory, users):
    _, st = _state(factory, users, orphan_grace=0.3)
    run = st.mgr.submit(users["a"].id, RunConfig(seed_smiles="CCO"), "orphan1")
    assert _wait(lambda: run.finished)
    assert run.status == "cancelled" and run.events[-1]["type"] == "error"
    assert "no client was listening" in run.events[-1]["payload"]["message"]


def test_an_attached_reader_keeps_the_run_and_detaching_arms_the_timer_again(factory, users):
    _, st = _state(factory, users, orphan_grace=0.4)
    st.settings.mock_delay = 0.5
    run = st.mgr.submit(users["a"].id, RunConfig(seed_smiles="CCO"), "kept1")
    st.mgr.attach(run)
    time.sleep(0.9)
    assert not run.finished or run.status == "done"  # a reader is attached: the orphan timer did not fire
    if not run.finished:
        st.mgr.detach(run)
        assert _wait(lambda: run.finished) and run.status in ("cancelled", "done")


def test_a_finished_run_is_not_touched_and_zero_grace_disables_the_cancel(factory, users):
    _, st = _state(factory, users, orphan_grace=0.2)
    st.settings.mock_delay = 0.0
    run = st.mgr.submit(users["a"].id, RunConfig(seed_smiles="CCO"), "quick1")
    assert _wait(lambda: run.finished) and run.status == "done"
    time.sleep(0.4)
    assert run.status == "done"
    _, st2 = _state(factory, users, orphan_grace=0.0)
    st2.settings.mock_delay = 0.3
    r2 = st2.mgr.submit(users["a"].id, RunConfig(seed_smiles="CCO"), "nocancel")
    time.sleep(0.5)
    assert r2.status != "cancelled"
    assert _wait(lambda: r2.finished, 10)


def test_stream_slot_is_released_when_the_response_ends_and_when_it_never_started(factory, users):
    app, st = _state(factory, users, orphan_grace=5.0)
    c = TestClient(app)
    rid = c.post("/lead/api/runs", json={"seed_smiles": NITRO}, headers=H("a")).json()["run_id"]
    r = c.get(f"/lead/api/runs/{rid}/events", headers=H("a"))
    assert r.status_code == 200 and "event: run_end" in r.text
    assert st.mgr._streams == {} and st.mgr.runs[rid].streams == 0
    # a response whose body never started (client vanished first) still gives the slot back
    import asyncio

    from leadstudio.server import routes

    released = []

    async def go():
        resp = routes._ReleasingStream(iter(()), lambda: released.append(1), media_type="text/event-stream")

        async def receive():
            return {"type": "http.disconnect"}

        async def send(_m):
            raise OSError("client gone")

        with pytest.raises(OSError):
            await resp({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)

    asyncio.run(go())
    assert released == [1]


def test_stream_cap_slots_come_back_so_a_user_is_not_locked_out(factory, users):
    app, st = _state(factory, users, per_user_streams=1, orphan_grace=5.0)
    c = TestClient(app)
    rid = c.post("/lead/api/runs", json={"seed_smiles": NITRO}, headers=H("a")).json()["run_id"]
    for _ in range(4):  # four sequential streams on a cap of one: each must release before the next
        assert c.get(f"/lead/api/runs/{rid}/events", headers=H("a")).status_code == 200


# ---------------------------------------------------------------- no structure in a log call
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
_STRUCT = re.compile(r"smiles|smi\b|molblock|\bsdf\b|\bmol\b|canonical|\binchi|seed_mol|child|parent_s|pdb_text|\bcontent\b", re.I)


def _log_calls(path: pathlib.Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _LOG_METHODS:
            base = node.func.value
            name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
            if re.search(r"^(_?log|_?logger|logging)$", name or "", re.I):
                yield node


def test_no_log_call_in_the_service_code_mentions_a_structure():
    offenders = []
    files = [p for d in ("leadstudio", "backend") for p in (ROOT / d).rglob("*.py") if "molmods" not in p.parts]
    n = 0
    for p in files:
        for call in _log_calls(p):
            n += 1
            for arg in call.args[1:] if call.args else []:  # args after the format string are the interpolated values
                if _STRUCT.search(ast.unparse(arg)):
                    offenders.append(f"{p.relative_to(ROOT)}:{call.lineno}: {ast.unparse(call)[:120]}")
            fmt = call.args[0] if call.args else None
            literal = re.sub(r"%[sdrf.0-9]*", "", fmt.value) if isinstance(fmt, ast.Constant) and isinstance(fmt.value, str) else ""
            if _STRUCT.search(literal) or isinstance(fmt, ast.JoinedStr | ast.BinOp | ast.Call) and _STRUCT.search(ast.unparse(fmt)):
                offenders.append(f"{p.relative_to(ROOT)}:{call.lineno}: {ast.unparse(call)[:120]}")
    assert n > 25, "the scan found too few log calls: the matcher is broken"
    assert not offenders, "\n".join(offenders)


def test_a_full_mock_run_logs_no_structure(factory, users, caplog):
    import logging

    _, st = _state(factory, users, orphan_grace=5.0)
    st.settings.mock_delay = 0.0
    with caplog.at_level(logging.DEBUG):
        run = st.mgr.submit(users["a"].id, RunConfig(seed_smiles=NITRO), "logrun1")
        assert _wait(lambda: run.finished)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert text and NITRO not in text and "c1ccc" not in text
