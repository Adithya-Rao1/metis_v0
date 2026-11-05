"""Migration 0006 (Wave 4 hardening), exercised as the real roles. `su` only seeds and inspects."""

import uuid

import psycopg
import pytest

from test_schema_security import H, mkproj, register

HASH_A = "$2b$04$" + "a" * 53
HASH_B = "$2b$04$" + "b" * 53


def as_role(pg, role, uid=None):
    c = psycopg.connect(pg, autocommit=True)
    c.execute(f"SET ROLE {role}")
    if uid:
        c.execute("SELECT set_config('metis.user_id', %s, false)", (str(uid),))
    return c


def due_request(su, days_ago=1, scope="account"):
    return su.execute(
        "INSERT INTO deletion_request (owner_id, scope, backups_purge_due) "
        "VALUES (gen_random_uuid(), %s, now() - make_interval(days => %s)) RETURNING id",
        (scope, days_ago),
    ).fetchone()[0]


# ---------------------------------------------------------------- F-129: deletion_due_for_purge / deletion_mark_purged
def test_due_list_works_for_readonly_and_operator_and_hides_owner_ids(pg, su):
    due = due_request(su)
    su.execute("INSERT INTO deletion_request (owner_id, scope) VALUES (gen_random_uuid(), 'account')")  # due in 30 days
    for role in ("metis_readonly", "metis_operator"):
        with as_role(pg, role) as c:
            rows = c.execute("SELECT * FROM deletion_due_for_purge()").fetchall()
            assert [r[0] for r in rows] == [due], role
            cols = [d.name for d in c.execute("SELECT * FROM deletion_due_for_purge()").description]
            assert cols == ["id", "scope", "requested_at", "backups_purge_due"]  # no owner id


def test_direct_select_as_readonly_sees_only_due_unpurged_rows(pg, su):
    due = due_request(su)
    su.execute("INSERT INTO deletion_request (owner_id, scope) VALUES (gen_random_uuid(), 'account')")
    purged = due_request(su, days_ago=40)
    su.execute("UPDATE deletion_request SET backups_purged_at = now() WHERE id = %s", (purged,))
    with as_role(pg, "metis_readonly") as c:
        rows = c.execute("SELECT id FROM deletion_request").fetchall()
        assert [r[0] for r in rows] == [due]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE deletion_request SET backups_purged_at = now()")


def test_mark_purged_only_for_operator_only_when_due_and_audits(pg, su):
    due = due_request(su)
    not_due = su.execute(
        "INSERT INTO deletion_request (owner_id, scope) VALUES (gen_random_uuid(), 'account') RETURNING id"
    ).fetchone()[0]
    for role in ("metis_readonly", "metis_app"):
        with as_role(pg, role) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT deletion_mark_purged(%s)", (due,))
    with as_role(pg, "metis_operator") as c:
        assert c.execute("SELECT deletion_mark_purged(%s)", (not_due,)).fetchone()[0] is False  # not due yet
        assert c.execute("SELECT deletion_mark_purged(%s)", (uuid.uuid4(),)).fetchone()[0] is False
        assert c.execute("SELECT deletion_mark_purged(%s)", (due,)).fetchone()[0] is True
        assert c.execute("SELECT deletion_mark_purged(%s)", (due,)).fetchone()[0] is False  # already recorded
        assert c.execute("SELECT count(*) FROM deletion_due_for_purge()").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT * FROM deletion_request")  # the operator role has no table access at all
    assert su.execute("SELECT backups_purged_at IS NOT NULL FROM deletion_request WHERE id = %s", (due,)).fetchone()[0]
    assert su.execute("SELECT backups_purged_at FROM deletion_request WHERE id = %s", (not_due,)).fetchone()[0] is None
    rows = su.execute("SELECT actor_id, action, resource FROM audit_log WHERE action = 'backups_purged'").fetchall()
    assert rows == [(None, "backups_purged", f"deletion_request:{due}")]


def test_app_role_still_cannot_read_or_touch_other_deletion_requests(pg, su):
    due_request(su)
    uid = register(su)
    with as_role(pg, "metis_app", uid) as c:
        assert c.execute("SELECT count(*) FROM deletion_request").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT * FROM deletion_due_for_purge()")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE deletion_request SET backups_purged_at = now()")


def test_new_roles_and_functions_are_minimal(su):
    for role in ("metis_retention", "metis_operator"):
        r = su.execute(
            "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication FROM pg_roles WHERE rolname = %s",
            (role,),
        ).fetchone()
        assert r == (False,) * 5, role
        assert su.execute(
            "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid WHERE r.rolname = %s", (role,)
        ).fetchone()[0] == 0
    for fn, owner in (
        ("deletion_due_for_purge()", "metis_retention"),
        ("deletion_mark_purged(uuid)", "metis_retention"),
        ("auth_replace_unverified(uuid,text)", "metis_auth"),
        ("auth_verify_email(text,text)", "metis_auth"),
        ("auth_retire_reset_tokens(uuid)", "metis_auth"),
    ):
        own, secdef, cfg = su.execute(
            "SELECT pg_get_userbyid(proowner), prosecdef, proconfig::text FROM pg_proc WHERE oid = %s::regprocedure", (fn,)
        ).fetchone()
        assert (own, secdef) == (owner, True) and "search_path" in cfg, fn
        assert not su.execute("SELECT has_function_privilege('public', %s::regprocedure, 'EXECUTE')", (fn,)).fetchone()[0], fn
    assert not su.execute(
        "SELECT has_function_privilege('metis_readonly', 'deletion_mark_purged(uuid)'::regprocedure, 'EXECUTE')"
    ).fetchone()[0]
    assert not su.execute(
        "SELECT has_function_privilege('metis_operator', 'auth_replace_unverified(uuid,text)'::regprocedure, 'EXECUTE')"
    ).fetchone()[0]
    privs = {
        (t, p)
        for t, p in su.execute(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants WHERE grantee = 'metis_retention'"
        ).fetchall()
    }
    assert privs == {("audit_log", "INSERT")}  # table-level; the deletion_request grants are column-level


# ---------------------------------------------------------------- F-122: unverified registrations
def test_replace_unverified_changes_only_unverified_accounts(pg, su):
    uid = register(su, "u@x.org")
    vid = register(su, "v@x.org")
    su.execute("UPDATE app_user SET email_verified_at = now() WHERE id = %s", (vid,))
    su.execute("SELECT auth_issue_token(%s, 'verify_email', %s, interval '1 hour')", (uid, "1" * 64))
    with as_role(pg, "metis_app") as c:
        assert c.execute("SELECT auth_replace_unverified(%s, %s)", (uid, HASH_B)).fetchone()[0] is True
        assert c.execute("SELECT auth_replace_unverified(%s, %s)", (vid, HASH_B)).fetchone()[0] is False
        assert c.execute("SELECT auth_replace_unverified(%s, %s)", (uuid.uuid4(), HASH_B)).fetchone()[0] is False
    assert su.execute("SELECT password_hash FROM app_user WHERE id = %s", (uid,)).fetchone()[0] == HASH_B
    assert su.execute("SELECT password_hash FROM app_user WHERE id = %s", (vid,)).fetchone()[0] != HASH_B
    assert su.execute("SELECT used_at IS NOT NULL FROM auth_token WHERE owner_id = %s", (uid,)).fetchone()[0]
    assert su.execute("SELECT count(*) FROM audit_log WHERE action = 'register_replace' AND actor_id = %s", (uid,)).fetchone()[0] == 1


def test_verify_email_optionally_sets_the_password(pg, su):
    a, b = register(su, "a@x.org"), register(su, "b@x.org")
    su.execute("SELECT auth_issue_token(%s, 'verify_email', %s, interval '1 hour')", (a, "a" * 64))
    su.execute("SELECT auth_issue_token(%s, 'verify_email', %s, interval '1 hour')", (b, "b" * 64))
    before_a = su.execute("SELECT password_hash FROM app_user WHERE id = %s", (a,)).fetchone()[0]
    with as_role(pg, "metis_app") as c:
        assert c.execute("SELECT auth_verify_email(%s)", ("a" * 64,)).fetchone()[0] == a          # token only: hash kept
        assert c.execute("SELECT auth_verify_email(%s, %s)", ("b" * 64, HASH_B)).fetchone()[0] == b
        assert c.execute("SELECT auth_verify_email(%s, %s)", ("a" * 64, HASH_B)).fetchone()[0] is None   # single use
        assert c.execute("SELECT auth_verify_email(%s, %s)", ("c" * 64, HASH_B)).fetchone()[0] is None   # unknown
    assert su.execute("SELECT password_hash, email_verified_at IS NOT NULL FROM app_user WHERE id = %s", (a,)).fetchone() == (before_a, True)
    assert su.execute("SELECT password_hash FROM app_user WHERE id = %s", (b,)).fetchone()[0] == HASH_B


# ---------------------------------------------------------------- F-125: reset tokens
def test_reset_retires_other_tokens_and_retire_function_is_self_only(pg, su):
    uid, other = register(su, "r@x.org"), register(su, "o@x.org")
    for u in (uid, other):
        su.execute("UPDATE app_user SET email_verified_at = now() WHERE id = %s", (u,))
    for i, u in enumerate((uid, uid, other)):
        su.execute("SELECT auth_issue_token(%s, 'reset_password', %s, interval '1 hour')", (u, str(i) * 64))
    with as_role(pg, "metis_app") as c:
        assert c.execute("SELECT auth_reset_password(%s, %s)", ("0" * 64, HASH_B)).fetchone()[0] == uid
        assert c.execute("SELECT auth_reset_password(%s, %s)", ("1" * 64, HASH_B)).fetchone()[0] is None  # retired
    assert su.execute("SELECT count(*) FROM auth_token WHERE owner_id = %s AND used_at IS NULL", (other,)).fetchone()[0] == 1
    # retire function: only for the identity set on the connection
    su.execute("SELECT auth_issue_token(%s, 'reset_password', %s, interval '1 hour')", (other, "9" * 64))
    with as_role(pg, "metis_app", uid) as c:
        assert c.execute("SELECT auth_retire_reset_tokens(%s)", (other,)).fetchone()[0] == 0
    assert su.execute("SELECT count(*) FROM auth_token WHERE owner_id = %s AND used_at IS NULL", (other,)).fetchone()[0] == 2
    with as_role(pg, "metis_app", other) as c:
        assert c.execute("SELECT auth_retire_reset_tokens(%s)", (other,)).fetchone()[0] == 2
    assert su.execute("SELECT count(*) FROM auth_token WHERE owner_id = %s AND used_at IS NULL", (other,)).fetchone()[0] == 0


# ---------------------------------------------------------------- F-126: job ownership columns
def test_job_worker_columns_and_privileges(pg, su):
    uid = register(su, "j@x.org")
    pid = mkproj(su, uid)
    wid = uuid.uuid4()
    with as_role(pg, "metis_app", uid) as c:
        jid = c.execute(
            "INSERT INTO job (owner_id, project_id, kind, worker_id, heartbeat_at) VALUES (%s, %s, 'predict', %s, now()) RETURNING id",
            (uid, pid, wid),
        ).fetchone()[0]
        c.execute("UPDATE job SET heartbeat_at = now() WHERE id = %s", (jid,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE job SET worker_id = gen_random_uuid() WHERE id = %s", (jid,))  # set at insert only
    assert su.execute("SELECT worker_id FROM job WHERE id = %s", (jid,)).fetchone()[0] == wid
