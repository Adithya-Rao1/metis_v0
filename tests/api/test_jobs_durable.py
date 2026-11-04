"""Durable job records: state survives a restart, other users get 404, the quota is counted from the DB."""

import threading
import uuid

import pytest
from api_support import FakeRegistry, H

from backend.jobs import deps
from backend.jobs.queue import MAX_ACTIVE_PER_USER, InProcessJobQueue, QuotaExceeded


def _row(su, jid):
    return su.execute("SELECT state, progress, error_code, started_at, finished_at FROM job WHERE id = %s", (jid,)).fetchone()


def test_job_row_written_and_get_survives_restart(client, queue, loaded, factory, su):
    pid, _ = loaded
    r = client.post(f"/projects/{pid}/predict", json={"endpoints": ["logd", "ames"]}, headers=H("a"))
    jid = r.json()["job_id"]
    queue.wait(uuid.UUID(jid))
    live = client.get(f"/jobs/{jid}", headers=H("a")).json()
    state, progress, err, started, finished = _row(su, jid)
    assert (state, progress, err) == ("succeeded", 1.0, None) and started and finished
    # "restart": a brand-new queue instance over the same database
    fresh = InProcessJobQueue(factory=factory)
    client.app.dependency_overrides[deps.get_queue] = lambda: fresh
    after = client.get(f"/jobs/{jid}", headers=H("a")).json()
    assert after["state"] == "done" and after["done"] == after["total"] == 6
    assert after["error"] is None and after["project_id"] == pid
    assert sorted(after["prediction_ids"]) == sorted(live["prediction_ids"]) and len(after["prediction_ids"]) == 6


def test_restart_marks_orphaned_jobs_failed(client, loaded, factory, su, users):
    pid, _ = loaded
    owner = users["a"].id
    ids = {}
    for state in ("queued", "running"):
        ids[state] = su.execute(
            "INSERT INTO job (owner_id, project_id, kind, state) VALUES (%s, %s, 'predict', %s) RETURNING id",
            (owner, pid, state),
        ).fetchone()[0]
    fresh = InProcessJobQueue(factory=factory)
    client.app.dependency_overrides[deps.get_queue] = lambda: fresh
    for state, jid in ids.items():
        body = client.get(f"/jobs/{jid}", headers=H("a")).json()
        assert body["state"] == "failed" and body["error"] == "interrupted by restart", state
        assert _row(su, jid)[:3] == ("failed", 0.0, "interrupted_by_restart")
        assert _row(su, jid)[4] is not None


def test_submit_reaps_orphans_so_quota_recovers_after_restart(factory, su, users, client, loaded):
    pid, _ = loaded
    owner = users["a"].id
    for _ in range(MAX_ACTIVE_PER_USER):
        su.execute("INSERT INTO job (owner_id, project_id, kind, state) VALUES (%s, %s, 'predict', 'running')", (owner, pid))
    q = InProcessJobQueue(factory=factory)
    rec = q.submit(owner, uuid.UUID(pid), 1, 0.0, lambda p: None, max_active=MAX_ACTIVE_PER_USER)
    q.wait(rec.id)
    assert su.execute("SELECT count(*) FROM job WHERE state = 'failed'").fetchone()[0] == MAX_ACTIVE_PER_USER
    q.shutdown()


def test_live_jobs_are_not_reaped(factory, users, client, loaded, su):
    pid, _ = loaded
    owner, ev = users["a"].id, threading.Event()
    q = InProcessJobQueue(max_workers=1, factory=factory)
    rec = q.submit(owner, uuid.UUID(pid), 1, 0.0, lambda p: ev.wait(10))
    got = q.get(owner, rec.id)
    assert got is not None and got.state in ("queued", "running")
    assert _row(su, rec.id)[0] in ("queued", "running")
    ev.set()
    q.shutdown()
    assert _row(su, rec.id)[0] == "succeeded"


def test_cross_user_isolation_is_404_even_after_restart(client, queue, loaded, factory):
    pid, _ = loaded
    jid = client.post(f"/projects/{pid}/predict", json={"endpoints": ["logd"]}, headers=H("a")).json()["job_id"]
    queue.wait(uuid.UUID(jid))
    assert client.get(f"/jobs/{jid}", headers=H("b")).status_code == 404  # served from memory
    fresh = InProcessJobQueue(factory=factory)
    client.app.dependency_overrides[deps.get_queue] = lambda: fresh
    assert client.get(f"/jobs/{jid}", headers=H("b")).status_code == 404  # served from the DB
    assert client.get(f"/jobs/{jid}", headers=H("a")).status_code == 200
    assert client.get(f"/jobs/{uuid.uuid4()}", headers=H("a")).status_code == 404


def test_quota_is_counted_from_the_db_across_queue_instances(factory, users, client, loaded):
    pid, _ = loaded
    owner, ev = users["a"].id, threading.Event()
    first = InProcessJobQueue(max_workers=1, factory=factory)
    for _ in range(MAX_ACTIVE_PER_USER):
        first.submit(owner, uuid.UUID(pid), 1, 0.0, lambda p: ev.wait(10), max_active=MAX_ACTIVE_PER_USER)
    with pytest.raises(QuotaExceeded):
        first.submit(owner, uuid.UUID(pid), 1, 0.0, lambda p: None, max_active=MAX_ACTIVE_PER_USER)
    # a second instance cannot see the first one's threads, but live-ness is decided by THIS instance's memory:
    # its rows are reaped, which is exactly the one-process assumption documented in docs/OPERATIONS.md.
    ev.set()
    first.shutdown()
    again = InProcessJobQueue(factory=factory)
    rec = again.submit(owner, uuid.UUID(pid), 1, 0.0, lambda p: None, max_active=MAX_ACTIVE_PER_USER)
    again.wait(rec.id)
    again.shutdown()


def test_quota_http_429_from_db_counts(client, loaded, factory, app):
    pid, _ = loaded
    ev = threading.Event()

    class Slow(FakeRegistry):
        def predict(self, *a, **k):
            ev.wait(10)
            return super().predict(*a, **k)

    q = InProcessJobQueue(max_workers=1, factory=factory)
    app.dependency_overrides[deps.get_registry] = lambda: Slow()
    app.dependency_overrides[deps.get_queue] = lambda: q
    codes = [client.post(f"/projects/{pid}/predict", json={"endpoints": ["logd"]}, headers=H("a")).status_code for _ in range(MAX_ACTIVE_PER_USER + 2)]
    ev.set()
    q.shutdown()
    assert codes[:MAX_ACTIVE_PER_USER] == [202] * MAX_ACTIVE_PER_USER and codes[MAX_ACTIVE_PER_USER:] == [429] * 2


def test_failed_job_stores_class_name_only(factory, users, client, loaded, su):
    pid, _ = loaded
    owner = users["a"].id
    q = InProcessJobQueue(factory=factory)

    def boom(progress):
        raise ValueError("CC(=O)Oc1ccccc1C(=O)O secret smiles")

    rec = q.submit(owner, uuid.UUID(pid), 1, 0.0, boom)
    q.wait(rec.id)
    state, _, code, *_ = _row(su, rec.id)
    assert (state, code) == ("failed", "job_failed_ValueError")
    fresh = InProcessJobQueue(factory=factory)
    got = fresh.get(owner, rec.id)
    assert got is not None and got.error == "job failed (ValueError)" and "smiles" not in got.error
    q.shutdown()
    fresh.shutdown()
