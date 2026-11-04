"""Wave 3 red-team (Agent 10), durable jobs: cross-user access, structure leakage into rows/logs, quota, orphans and restart
reaping, and what a second API process does to live jobs. xfail(strict=True) = real defect; see private/qa_wave3.md."""
from __future__ import annotations

import json
import logging
import threading
import uuid

import pytest
import w2stack as S
from api_support import FakeRegistry
from w2stack import Cl

from backend.jobs.queue import InProcessJobQueue, QuotaExceeded

SMILES = "CCOC(=O)C1CCCCC1N(C)C"           # distinctive: must never appear in a job row or a log line


def defect(fid, sev, msg):
    return pytest.mark.xfail(strict=True, reason=f"{fid} {sev}: {msg}")


@pytest.fixture
def queues(w3):
    made = []

    def make(workers=2):
        q = InProcessJobQueue(max_workers=workers, factory=w3)
        made.append(q)
        return q

    yield make
    for q in made:
        q.shutdown(wait=True)


class Gate:
    """A job body that blocks until released, so tests can hold jobs in the running state."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, progress):
        self.started.set()
        self.release.wait(30)


def user_with_project(app, email="a@example.com"):
    c = Cl(app)
    uid = c.signup(email)
    pid = c.post("/projects", {"name": "p"}).json()["id"]
    return c, uid, uuid.UUID(pid)


class Boom(FakeRegistry):
    def predict(self, endpoint, canonical_smiles, ensemble_n):
        raise ValueError(f"cannot featurise {canonical_smiles}")


# ----------------------------------------------------------------------------------------------- isolation
def test_other_users_get_404_for_a_job_and_cannot_attach_jobs_to_it(mk3, queues, su3):
    q = queues()
    app = mk3(queue=q)
    a, ua, pa = user_with_project(app)
    b = Cl(app)
    ub = b.signup("b@example.com")
    assert a.post(f"/projects/{pa}/compounds", {"smiles": [SMILES]}).status_code == 201
    r = a.post(f"/projects/{pa}/predict", {"endpoints": ["logd"], "ensemble_n": 5})
    assert r.status_code == 202
    jid = r.json()["job_id"]
    q.wait(uuid.UUID(jid))
    assert a.get(f"/jobs/{jid}").json()["state"] == "done"
    miss = b.get(f"/jobs/{jid}")
    nonexistent = b.get(f"/jobs/{uuid.uuid4()}")
    assert (miss.status_code, miss.json()) == (nonexistent.status_code, nonexistent.json()) == (404, {"detail": "not found"})
    assert b.post(f"/projects/{pa}/predict", {"endpoints": ["logd"], "ensemble_n": 5}).status_code == 404
    # restart: a fresh queue on the same database still answers the owner, never the other user
    q2 = queues()
    assert q2.get(ub, uuid.UUID(jid)) is None
    got = q2.get(ua, uuid.UUID(jid))
    assert got is not None and got.state == "done" and got.done == got.total == 1
    # the database refuses a job for someone else's project even if the application layer were bypassed
    with pytest.raises(Exception):
        q2.submit(ub, pa, 1, 1.0, lambda p: None)
    assert su3.execute("select count(*) from job where owner_id = %s", (str(ub),)).fetchone()[0] == 0


# ---------------------------------------------------------------------------- no structures in rows or logs
def test_failed_job_stores_and_shows_the_exception_class_only(mk3, queues, su3, caplog):
    q = queues()
    app = mk3(queue=q, registry=Boom())
    a, ua, pa = user_with_project(app)
    assert a.post(f"/projects/{pa}/compounds", {"smiles": [SMILES]}).status_code == 201
    canon = su3.execute("select canonical_smiles from compound").fetchone()[0]
    with caplog.at_level(logging.DEBUG):
        r = a.post(f"/projects/{pa}/predict", {"endpoints": ["logd"], "ensemble_n": 5})
        jid = r.json()["job_id"]
        q.wait(uuid.UUID(jid))
        j = a.get(f"/jobs/{jid}").json()
        q2 = queues()
        again = q2.get(ua, uuid.UUID(jid))
    assert j["state"] == "failed" and j["error"] == "job failed (ValueError)" and again.error == "job failed (ValueError)"
    row = su3.execute("select to_jsonb(j) from job j").fetchone()[0]
    blob = json.dumps(row) + json.dumps(j) + "\n".join(f"{x.name} {x.getMessage()} {x.exc_text or ''}" for x in caplog.records)
    for secret in (SMILES, canon, "featurise"):
        assert secret not in blob
    assert row["error_code"] == "job_failed_ValueError" and len(row["error_code"]) <= 64


def test_job_table_has_no_free_text_column_that_could_hold_a_structure(su3):
    cols = su3.execute("select column_name, data_type from information_schema.columns where table_name = 'job'").fetchall()
    text_cols = {c for c, t in cols if t in ("text", "character varying", "jsonb", "json")}
    assert text_cols == {"kind", "state", "error_code"}
    cks = " ".join(r[0] for r in su3.execute("select pg_get_constraintdef(oid) from pg_constraint where conrelid = 'job'::regclass and contype = 'c'").fetchall())
    assert "length(error_code) <= 64" in cks


# --------------------------------------------------------------------------------------------------- quota
def test_quota_is_counted_from_the_database_per_user_and_survives_a_new_queue(mk3, queues, su3):
    app = mk3()
    a, ua, pa = user_with_project(app)
    b, ub, pb = user_with_project(app, "b@example.com")
    q = queues(workers=1)
    gates = [Gate() for _ in range(3)]
    try:
        for g in gates[:2]:
            q.submit(ua, pa, 1, 1.0, g, max_active=2)
        with pytest.raises(QuotaExceeded):
            q.submit(ua, pa, 1, 1.0, gates[2], max_active=2)
        q.submit(ub, pb, 1, 1.0, gates[2], max_active=2)                    # the other user has their own allowance
        # a second queue object sees the same two active rows in the database (live in THIS process, so they are not reaped)
        q_same_process_view = q
        with pytest.raises(QuotaExceeded):
            q_same_process_view.submit(ua, pa, 1, 1.0, Gate(), max_active=2)
    finally:
        for g in gates:
            g.release.set()
    for jid in list(q._futures):
        q.wait(jid)
    assert q.submit(ua, pa, 1, 1.0, lambda p: None, max_active=2)           # finished jobs free the quota


def test_rows_left_by_a_dead_process_are_failed_not_counted_and_never_lock_the_owner_out(mk3, queues, su3):
    app = mk3()
    a, ua, pa = user_with_project(app)
    for _ in range(3):
        su3.execute("insert into job (owner_id, project_id, kind, state) values (%s,%s,'predict','running')", (str(ua), str(pa)))
    q = queues()
    rec = q.submit(ua, pa, 1, 1.0, lambda p: None, max_active=1)             # would be refused if the orphans counted
    assert rec is not None
    states = [r[0] for r in su3.execute("select error_code from job where state = 'failed'").fetchall()]
    assert states == ["interrupted_by_restart"] * 3
    got = q.get(ua, rec.id)
    assert got is not None


@defect("F-127", "LOW", "the per-user quota is read from job rows, and those rows are deleted by ON DELETE CASCADE when the project is deleted while its jobs are still queued/running in the thread pool: create jobs, delete the project, create more, and the user has more running/queued jobs than the quota allows (each ends at its first insert, but it still occupies a worker and runs one inference)")
def test_quota_cannot_be_reset_by_deleting_the_project_while_jobs_are_running(mk3, queues):
    app = mk3()
    c, uid, p1 = user_with_project(app)
    p2 = uuid.UUID(c.post("/projects", {"name": "second"}).json()["id"])
    q = queues(workers=1)
    gates = [Gate() for _ in range(4)]
    try:
        q.submit(uid, p1, 1, 1.0, gates[0], max_active=2)
        q.submit(uid, p1, 1, 1.0, gates[1], max_active=2)
        assert c.delete(f"/projects/{p1}").status_code == 204
        with pytest.raises(QuotaExceeded):
            q.submit(uid, p2, 1, 1.0, gates[2], max_active=2)
    finally:
        for g in gates:
            g.release.set()
        for jid in list(q._futures):
            q.wait(jid)


# ------------------------------------------------------------------------------------ second API process
@defect("F-126", "MEDIUM", "any second queue object on the same database (a second uvicorn worker, or the old container during a rolling restart) fails the owner's LIVE jobs as 'interrupted by restart' the first time the owner polls through it, because 'live' is defined as 'tracked by this process'; the first process then still finishes the work and overwrites the row, so the state flip-flops. docs/OPERATIONS.md states the single-process assumption but nothing enforces it (no advisory lock, no lease)")
def test_a_second_api_process_must_not_fail_jobs_the_first_is_still_running(mk3, queues):
    app = mk3()
    c, uid, pid = user_with_project(app)
    first, second = queues(), queues()
    g = Gate()
    rec = first.submit(uid, pid, 1, 1.0, g)
    assert g.started.wait(10)
    try:
        seen = second.get(uid, rec.id)
        assert seen is not None and seen.state in ("queued", "running"), seen
    finally:
        g.release.set()
        first.wait(rec.id)


def test_a_second_process_does_reap_only_the_requesting_owners_rows(mk3, queues, su3):
    """Blast radius of F-126 is the owner's own jobs: user B's live job is untouched when A polls through the second process."""
    app = mk3()
    a, ua, pa = user_with_project(app)
    b, ub, pb = user_with_project(app, "b@example.com")
    first, second = queues(), queues()
    ga, gb = Gate(), Gate()
    ra, rb = first.submit(ua, pa, 1, 1.0, ga), first.submit(ub, pb, 1, 1.0, gb)
    try:
        assert ga.started.wait(10) and gb.started.wait(10)
        second.get(ua, ra.id)
        states = dict(su3.execute("select owner_id::text, state from job").fetchall())
        assert states[str(ub)] == "running"
    finally:
        ga.release.set()
        gb.release.set()
        first.wait(ra.id)
        first.wait(rb.id)


def test_memory_stays_bounded_while_the_database_keeps_every_finished_job(mk3, queues):
    from backend.jobs import queue as jq

    app = mk3()
    c, uid, pid = user_with_project(app)
    q = queues()
    ids = [q.submit(uid, pid, 1, 1.0, lambda p: None).id for _ in range(jq.MAX_TRACKED + 40)]
    for i in ids:
        q.wait(i)
    q.submit(uid, pid, 1, 1.0, lambda p: None)
    assert len(q._jobs) <= jq.MAX_TRACKED + 1
    assert q.get(uid, ids[0]) is not None and q.get(uid, ids[0]).state == "done"          # evicted from memory, still readable
