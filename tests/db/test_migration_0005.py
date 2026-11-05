"""Migration 0005 (Lead Studio storage): structure, CHECK size limits and privileges. Runs as the least-privileged app
role (metis_app); `su` only seeds and inspects. Cross-user attacks are in tests/security/test_lead_rls.py."""
import json

import psycopg
import pytest
from test_schema_security import register

TABLES = ("lead_saved_run", "lead_manual_session", "lead_manual_step", "lead_profile")
EVENTS = json.dumps([{"type": "run_start", "run_id": "r", "seq": 0, "payload": {}}])


def rejected(c, sql, params=()):
    try:
        c.execute(sql, params)
        return False
    except psycopg.Error:
        c.rollback()
        return True


def test_tables_have_uuid_pk_not_null_cascading_owner_and_forced_rls(su):
    for t in TABLES:
        rls, force = su.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname=%s", (t,)).fetchone()
        assert rls and force, t
        assert su.execute("SELECT is_nullable, data_type FROM information_schema.columns WHERE table_name=%s AND column_name='owner_id'",
                          (t,)).fetchone() == ("NO", "uuid"), t
        assert su.execute("""SELECT format_type(a.atttypid, a.atttypmod) FROM pg_index i
                             JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                             WHERE i.indrelid = %s::regclass AND i.indisprimary""", (t,)).fetchone()[0] == "uuid", t
        # ON DELETE CASCADE to app_user (account deletion removes everything)
        assert su.execute("""SELECT confdeltype FROM pg_constraint WHERE conrelid=%s::regclass AND contype='f'
                             AND confrelid='app_user'::regclass""", (t,)).fetchone()[0] == "c", t
        assert su.execute("SELECT count(*) FROM pg_policies WHERE tablename=%s", (t,)).fetchone()[0] == 1, t


def test_only_metis_app_has_privileges_and_never_public_or_readonly(su):
    for t in TABLES:
        grantees = {g for (g,) in su.execute("SELECT DISTINCT grantee FROM information_schema.role_table_grants WHERE table_name=%s", (t,))}
        cols = {g for (g,) in su.execute("SELECT DISTINCT grantee FROM information_schema.role_column_grants WHERE table_name=%s", (t,))}
        assert (grantees | cols) - {"metis_app", su.execute("SELECT current_user").fetchone()[0]} == set(), t
        for role in ("metis_readonly", "metis_janitor", "metis_auth"):
            assert not su.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, t)).fetchone()[0], (role, t)
    # nobody can write timestamps or ids, and steps cannot be edited after the fact
    assert not su.execute("SELECT has_column_privilege('metis_app','lead_saved_run','created_at','INSERT')").fetchone()[0]
    assert not su.execute("SELECT has_column_privilege('metis_app','lead_saved_run','id','INSERT')").fetchone()[0]
    assert not su.execute("SELECT has_table_privilege('metis_app','lead_manual_step','UPDATE')").fetchone()[0]
    assert not su.execute("SELECT has_table_privilege('metis_app','lead_saved_run','UPDATE')").fetchone()[0]
    assert su.execute("SELECT has_column_privilege('metis_app','lead_profile','body','UPDATE')").fetchone()[0]
    assert not su.execute("SELECT has_column_privilege('metis_app','lead_profile','owner_id','UPDATE')").fetchone()[0]


@pytest.fixture
def me(app, su):
    uid = register(su, "lead@x.org")
    return uid, app.as_user(uid)


def test_saved_run_checks(me):
    uid, c = me
    ins = "INSERT INTO lead_saved_run(owner_id,title,stage,target_id,seed_smiles,headline,events) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)"
    ok = (uid, "t", "pharmacology", "egfr", "CCO", "h", EVENTS)
    c.execute(ins, ok)
    c.commit()
    for bad in ((uid, "", "pharmacology", None, None, None, EVENTS), (uid, "x" * 121, "pharmacology", None, None, None, EVENTS),
                (uid, "a\nb", "pharmacology", None, None, None, EVENTS), (uid, "t", "manual", None, None, None, EVENTS),
                (uid, "t", "pharmacology", "bad id", None, None, EVENTS), (uid, "t", "pharmacology", "x" * 65, None, None, EVENTS),
                (uid, "t", "pharmacology", None, "C" * 2001, None, EVENTS), (uid, "t", "pharmacology", None, None, "h" * 301, EVENTS),
                (uid, "t", "pharmacology", None, None, None, "{}"), (uid, "t", "pharmacology", None, None, None, "[]"),
                (uid, "t", "pharmacology", None, None, None, json.dumps([{"a": 1}] * 5001)),
                (uid, "t", "pharmacology", None, None, None, json.dumps([{"blob": "x" * 2_000_000}]))):
        assert rejected(c, ins, bad), bad[1:6]


def test_manual_checks(me):
    uid, c = me
    sid = c.execute("INSERT INTO lead_manual_session(owner_id,seed_smiles) VALUES (%s,'CCO') RETURNING id", (uid,)).fetchone()[0]
    c.commit()
    ins = ("INSERT INTO lead_manual_step(owner_id,session_id,seq,parent_smiles,child_smiles,edit_id,site,reward_delta,t) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)")
    c.execute(ins, (uid, sid, 0, "CCO", "CCN", "e1", [1, 2], 0.5, 1.0))
    c.commit()
    for bad in ((uid, sid, 0, "CCO", "CCN", "e1", [1], 0.5, 1.0),                      # duplicate seq
                (uid, sid, -1, "CCO", "CCN", "e1", [1], 0.5, 1.0), (uid, sid, 500, "CCO", "CCN", "e1", [1], 0.5, 1.0),
                (uid, sid, 1, "", "CCN", "e1", [1], 0.5, 1.0), (uid, sid, 1, "C" * 2001, "CCN", "e1", [1], 0.5, 1.0),
                (uid, sid, 1, "CCO", "CCN", "bad id", [1], 0.5, 1.0), (uid, sid, 1, "CCO", "CCN", "", [1], 0.5, 1.0),
                (uid, sid, 1, "CCO", "CCN", "e1", list(range(65)), 0.5, 1.0),
                (uid, sid, 1, "CCO", "CCN", "e1", [1], float("nan"), 1.0), (uid, sid, 1, "CCO", "CCN", "e1", [1], float("inf"), 1.0),
                (uid, sid, 1, "CCO", "CCN", "e1", [1], 1e9, 1.0), (uid, sid, 1, "CCO", "CCN", "e1", [1], 0.5, -1.0),
                (uid, sid, 1, "CCO", "CCN", "e1", [1], 0.5, float("nan"))):
        assert rejected(c, ins, bad), bad[2:]
    assert rejected(c, "INSERT INTO lead_manual_session(owner_id,seed_smiles) VALUES (%s,'')", (uid,))
    assert rejected(c, "INSERT INTO lead_manual_session(owner_id,seed_smiles,target_id) VALUES (%s,'CCO','a b')", (uid,))
    # deleting the session deletes its steps
    assert c.execute("DELETE FROM lead_manual_session WHERE id=%s", (sid,)).rowcount == 1
    c.commit()
    assert c.execute("SELECT count(*) FROM lead_manual_step").fetchone()[0] == 0


def test_profile_checks_and_update_surface(me):
    uid, c = me
    ins = "INSERT INTO lead_profile(owner_id,name,body) VALUES (%s,%s,%s::jsonb)"
    pid = c.execute(ins + " RETURNING id", (uid, "p", "{}")).fetchone()[0]
    c.commit()
    for bad in ((uid, "", "{}"), (uid, "x" * 81, "{}"), (uid, "a\tb", "{}"), (uid, "p", "[]"), (uid, "p", "1"),
                (uid, "p", json.dumps({"k": "x" * 20000}))):
        assert rejected(c, ins, bad), bad[1:]
    assert c.execute("UPDATE lead_profile SET name='q', body='{\"a\":1}'::jsonb, updated_at=now() WHERE id=%s", (pid,)).rowcount == 1
    c.commit()
    assert rejected(c, "UPDATE lead_profile SET created_at = now() - interval '1 year' WHERE id=%s", (pid,))
    assert rejected(c, "UPDATE lead_profile SET body = %s::jsonb WHERE id=%s", (json.dumps({"k": "x" * 20000}), pid))
