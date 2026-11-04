import logging
import uuid

import pytest

from backend.maintenance import run_once, runner

HASH = "$2b$12$" + "a" * 53


def _user(su, email, age_days, verified):
    return su.execute(
        "INSERT INTO app_user (email_lower, password_hash, created_at, email_verified_at) "
        "VALUES (%s, %s, now() - make_interval(days => %s), CASE WHEN %s THEN now() ELSE NULL END) RETURNING id",
        (email, HASH, age_days, verified),
    ).fetchone()[0]


def _seed(su):
    su.execute(
        "INSERT INTO rate_limit_bucket (key_hash, window_start) VALUES (%s, now() - interval '3 hours'), "
        "(%s, now())",
        ("a" * 64, "b" * 64),
    )
    stale = _user(su, "stale-secret@example.org", 10, False)
    _user(su, "fresh-unverified@example.org", 2, False)
    _user(su, "old-verified@example.org", 400, True)
    ok = _user(su, "session-owner@example.org", 1, True)
    su.execute(
        "INSERT INTO session_revocation (jti, owner_id, expires_at) VALUES (%s, %s, now() - interval '1 hour'), "
        "(%s, %s, now() + interval '1 hour')",
        (uuid.uuid4(), ok, uuid.uuid4(), ok),
    )
    return stale


def test_run_once_purges_only_what_is_stale(factory, su, caplog):
    _seed(su)
    caplog.set_level(logging.INFO, logger="metis.maintenance")
    counts = run_once(factory)
    assert counts == {"rate_limit_buckets": 1, "session_revocations": 1, "unverified_accounts": 1}
    assert su.execute("SELECT count(*) FROM rate_limit_bucket").fetchone()[0] == 1
    assert su.execute("SELECT count(*) FROM session_revocation").fetchone()[0] == 1
    left = {r[0] for r in su.execute("SELECT email_lower FROM app_user")}
    assert left == {"fresh-unverified@example.org", "old-verified@example.org", "session-owner@example.org"}
    # the purge is audited by the database function (id only, in the audit log), never in process logs
    assert su.execute("SELECT count(*) FROM audit_log WHERE action = 'purge_unverified'").fetchone()[0] == 1
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "rate_limit_buckets=1" in text and "unverified_accounts=1" in text
    assert "@" not in text and "secret" not in text


def test_run_once_is_idempotent(factory, su):
    _seed(su)
    run_once(factory)
    assert run_once(factory) == {"rate_limit_buckets": 0, "session_revocations": 0, "unverified_accounts": 0}


def test_failed_step_raises_without_hiding_others(factory, su, monkeypatch, caplog):
    _seed(su)
    steps = list(runner._STEPS)
    steps[1] = ("session_revocations", "SELECT no_such_function_for_test('user@example.org')")
    monkeypatch.setattr(runner, "_STEPS", tuple(steps))
    caplog.set_level(logging.INFO, logger="metis.maintenance")
    with pytest.raises(RuntimeError, match="session_revocations"):
        run_once(factory)
    # the other steps still ran
    assert su.execute("SELECT count(*) FROM rate_limit_bucket").fetchone()[0] == 1
    assert su.execute("SELECT count(*) FROM app_user").fetchone()[0] == 3
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "user@example.org" not in text and "no_such_function" not in text


def test_main_exit_codes(factory, su, monkeypatch):
    _seed(su)
    monkeypatch.setattr(runner, "make_engine", lambda: factory.kw["bind"])
    assert runner.main([]) == 0
    steps = ((("rate_limit_buckets", "SELECT no_such_function_for_test()"),))
    monkeypatch.setattr(runner, "_STEPS", steps)
    assert runner.main([]) == 1


def test_main_exit_code_when_unconfigured(monkeypatch):
    def boom():
        raise RuntimeError("METIS_DATABASE_URL is not set")

    monkeypatch.setattr(runner, "make_engine", boom)
    assert runner.main([]) == 2


def test_loop_interval_floor():
    with pytest.raises(SystemExit):
        runner.main(["--loop", "5"])


def test_loop_mode_runs_and_stops(factory, su, monkeypatch):
    calls = []

    def fake_run_once(f):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("x")
        return {}

    monkeypatch.setattr(runner, "run_once", fake_run_once)
    waits = []

    class Ev:
        def __init__(self):
            self.n = 0

        def is_set(self):
            return self.n >= 3

        def set(self):
            self.n = 99

        def wait(self, t):
            waits.append(t)
            self.n += 1

    monkeypatch.setattr(runner.threading, "Event", Ev)
    monkeypatch.setattr(runner.signal, "signal", lambda *a: None)
    rc = runner._loop(factory, 120)
    assert len(calls) == 3 and waits == [120, 120, 120] and rc == 0
