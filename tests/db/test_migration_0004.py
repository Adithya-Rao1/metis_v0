"""Migration 0004 (Wave 3 hardening): purge functions, lock expiry, password reset, unverified expiry.
Everything runs as the least-privileged app role (metis_app); `su` only seeds and inspects."""
import psycopg
import pytest

from test_schema_security import H, register

K = "c" * 64


def verified(su, email="v@x.org"):
    uid = register(su, email)
    su.execute("UPDATE app_user SET email_verified_at = now() WHERE id=%s", (uid,))
    return uid


def issue_reset(su, uid, token_hash=H, ttl="1 hour"):
    su.execute("INSERT INTO auth_token(owner_id,purpose,token_hash,expires_at) VALUES (%s,'reset_password',%s,now()+%s::interval)",
               (uid, token_hash, ttl))


def state(su, uid):
    return su.execute("SELECT failed_logins, locked_until IS NOT NULL, password_hash FROM app_user WHERE id=%s", (uid,)).fetchone()


# ---- rate_limit_purge (F-65)
def test_app_role_can_no_longer_delete_buckets_directly(app, su):
    c = app.as_user(None)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("DELETE FROM rate_limit_bucket")
    c.rollback()


def test_rate_limit_purge_only_removes_old_buckets(app, su):
    su.execute("INSERT INTO rate_limit_bucket(key_hash, window_start) VALUES (%s, now() - interval '3 hours'), (%s, now() - interval '30 minutes'), (%s, now())",
               ("a" * 64, "b" * 64, "c" * 64))
    c = app.as_user(None)
    assert c.execute("SELECT rate_limit_purge()").fetchone()[0] == 1  # the floor is inside the function
    c.commit()
    assert su.execute("SELECT count(*) FROM rate_limit_bucket").fetchone()[0] == 2
    assert c.execute("SELECT rate_limit_purge()").fetchone()[0] == 0
    c.rollback()
    su.execute("TRUNCATE rate_limit_bucket")


def test_rate_limit_purge_takes_no_arguments(app):
    c = app.as_user(None)
    with pytest.raises(psycopg.errors.UndefinedFunction):
        c.execute("SELECT rate_limit_purge(now())")
    c.rollback()


# ---- session_purge_expired
def test_session_purge_expired_removes_only_expired(app, su):
    uid = verified(su)
    su.execute("INSERT INTO session_revocation(jti, owner_id, expires_at) VALUES (gen_random_uuid(), %s, now() - interval '1 minute'), (gen_random_uuid(), %s, now() + interval '1 hour')", (uid, uid))
    c = app.as_user(None)
    assert c.execute("SELECT session_purge_expired()").fetchone()[0] == 1
    c.commit()
    assert su.execute("SELECT count(*) FROM session_revocation").fetchone()[0] == 1
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("DELETE FROM session_revocation")  # still no direct delete for the app role
    c.rollback()


# ---- auth_record_login (F-60)
def test_lapsed_lock_restarts_the_count(app, su):
    a = verified(su)
    c = app.as_user(None)
    for _ in range(5):
        c.execute("SELECT auth_record_login(%s,false)", (a,))
    c.commit()
    assert state(su, a)[:2] == (5, True)
    su.execute("UPDATE app_user SET locked_until = now() - interval '1 minute' WHERE id=%s", (a,))
    c.execute("SELECT auth_record_login(%s,false)", (a,))  # ONE miss after the lock lapsed
    c.commit()
    assert state(su, a)[:2] == (1, False)  # counted from zero, not re-locked
    for _ in range(3):
        c.execute("SELECT auth_record_login(%s,false)", (a,))
    c.commit()
    assert state(su, a)[:2] == (4, False)
    c.execute("SELECT auth_record_login(%s,false)", (a,))
    c.commit()
    assert state(su, a)[:2] == (5, True)  # the fifth consecutive miss locks again


def test_failure_in_active_lock_changes_nothing_and_writes_no_audit(app, su):
    a = verified(su)
    c = app.as_user(None)
    for _ in range(5):
        c.execute("SELECT auth_record_login(%s,false)", (a,))
    c.commit()
    n = su.execute("SELECT count(*) FROM audit_log WHERE actor_id=%s", (a,)).fetchone()[0]
    before = su.execute("SELECT failed_logins, locked_until FROM app_user WHERE id=%s", (a,)).fetchone()
    c.execute("SELECT auth_record_login(%s,false)", (a,))
    c.execute("SELECT auth_record_login(%s,true)", (a,))
    c.commit()
    assert su.execute("SELECT failed_logins, locked_until FROM app_user WHERE id=%s", (a,)).fetchone() == before
    assert su.execute("SELECT count(*) FROM audit_log WHERE actor_id=%s", (a,)).fetchone()[0] == n


def test_failure_on_lapsed_lock_writes_only_login_fail(app, su):
    a = verified(su)
    su.execute("UPDATE app_user SET failed_logins=5, locked_until = now() - interval '1 minute' WHERE id=%s", (a,))
    c = app.as_user(None)
    c.execute("SELECT auth_record_login(%s,false)", (a,))
    c.commit()
    acts = [r[0] for r in su.execute("SELECT action FROM audit_log WHERE actor_id=%s AND action LIKE 'login%%'", (a,)).fetchall()]
    assert acts == ["login_fail"]  # no spurious login_ok


# ---- auth_reset_password
def test_reset_sets_hash_clears_lock_and_is_single_use(app, su):
    a = verified(su)
    su.execute("UPDATE app_user SET failed_logins=5, locked_until = now() + interval '10 minutes' WHERE id=%s", (a,))
    issue_reset(su, a)
    new = "$2b$04$" + "n" * 53
    c = app.as_user(None)
    assert c.execute("SELECT auth_reset_password(%s,%s)", (H, new)).fetchone()[0] == a
    c.commit()
    assert state(su, a) == (0, False, new)
    assert su.execute("SELECT count(*) FROM audit_log WHERE actor_id=%s AND action='password_reset'", (a,)).fetchone()[0] == 1
    assert c.execute("SELECT auth_reset_password(%s,%s)", (H, "$2b$04$" + "z" * 53)).fetchone()[0] is None  # reused token
    c.commit()
    assert state(su, a)[2] == new


@pytest.mark.parametrize("case", ["unknown", "expired", "wrong_purpose", "unverified", "deleted"])
def test_reset_refuses_without_valid_proof(app, su, case):
    a = verified(su) if case != "unverified" else register(su, "u@x.org")
    if case == "expired":
        issue_reset(su, a, ttl="-1 hour")
    elif case == "wrong_purpose":
        su.execute("INSERT INTO auth_token(owner_id,purpose,token_hash,expires_at) VALUES (%s,'verify_email',%s,now()+interval '1 hour')", (a, H))
    elif case != "unknown":
        issue_reset(su, a)
    if case == "deleted":
        su.execute("UPDATE app_user SET deleted_at = now() WHERE id=%s", (a,))
    su.execute("UPDATE app_user SET failed_logins=5, locked_until = now() + interval '10 minutes' WHERE id=%s", (a,))
    before = state(su, a)
    c = app.as_user(None)
    assert c.execute("SELECT auth_reset_password(%s,%s)", (H, "$2b$04$" + "n" * 53)).fetchone()[0] is None
    c.commit()
    assert state(su, a) == before  # hash unchanged, lock NOT cleared


def test_reset_rejects_non_bcrypt_hash(app, su):
    a = verified(su)
    issue_reset(su, a)
    c = app.as_user(None)
    with pytest.raises(psycopg.errors.CheckViolation):
        c.execute("SELECT auth_reset_password(%s,%s)", (H, "plaintext"))
    c.rollback()


# ---- auth_purge_unverified (F-61)
def test_purge_unverified_has_a_one_day_floor_and_spares_verified(app, su):
    old_unv = register(su, "old@x.org")
    register(su, "new@x.org")
    mid_unv = register(su, "mid@x.org")
    ver = verified(su, "ver@x.org")
    su.execute("UPDATE app_user SET created_at = now() - interval '30 days' WHERE id IN (%s,%s)", (old_unv, ver))
    su.execute("UPDATE app_user SET created_at = now() - interval '2 hours' WHERE id=%s", (mid_unv,))
    c = app.as_user(None)
    # asking for a 1-second minimum age must NOT delete a registration younger than the 1-day floor
    assert c.execute("SELECT auth_purge_unverified(interval '1 second')").fetchone()[0] == 1
    c.commit()
    left = {r[0] for r in su.execute("SELECT email_lower FROM app_user").fetchall()}
    assert left == {"new@x.org", "mid@x.org", "ver@x.org"}
    assert su.execute("SELECT count(*) FROM audit_log WHERE action='purge_unverified'").fetchone()[0] == 1


OWNERS = {"rate_limit_purge()": "metis_janitor", "session_purge_expired()": "metis_janitor",
          "auth_purge_unverified(interval)": "metis_janitor", "auth_reset_password(text,text)": "metis_auth"}


def test_new_functions_are_not_executable_by_public_and_are_definer_owned(su):
    for fn, owner in OWNERS.items():
        row = su.execute("SELECT pg_get_userbyid(proowner), prosecdef, proconfig::text FROM pg_proc WHERE oid = %s::regprocedure", (fn,)).fetchone()
        assert row[0] == owner and row[1] is True and "search_path" in row[2], fn
        assert not su.execute("SELECT has_function_privilege('public', %s::regprocedure, 'EXECUTE')", (fn,)).fetchone()[0], fn
        assert su.execute("SELECT has_function_privilege('metis_app', %s::regprocedure, 'EXECUTE')", (fn,)).fetchone()[0], fn
        assert not su.execute("SELECT has_function_privilege('metis_readonly', %s::regprocedure, 'EXECUTE')", (fn,)).fetchone()[0], fn


def test_janitor_is_narrow_and_metis_auth_gained_nothing(su):
    assert su.execute("SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication FROM pg_roles WHERE rolname='metis_janitor'").fetchone() == (False,) * 5
    assert su.execute("SELECT pg_has_role('metis_app','metis_janitor','MEMBER')").fetchone()[0] is False
    # tenant data: no read access at all
    for t in ("project", "compound", "prediction", "measurement", "note", "consent", "grant_", "auth_token", "terms_acceptance"):
        assert su.execute("SELECT has_table_privilege('metis_janitor', %s, 'SELECT')", (t,)).fetchone()[0] is False, t
    # app_user: only the four columns the purge reads, never the password hash or email
    assert su.execute("SELECT has_column_privilege('metis_janitor','app_user','password_hash','SELECT')").fetchone()[0] is False
    assert su.execute("SELECT has_column_privilege('metis_janitor','app_user','email_lower','SELECT')").fetchone()[0] is False
    assert su.execute("SELECT has_table_privilege('metis_janitor','app_user','UPDATE')").fetchone()[0] is False
    # metis_auth still cannot delete users or the revocation/limit tables
    for t in ("app_user", "session_revocation", "rate_limit_bucket"):
        assert su.execute("SELECT has_table_privilege('metis_auth', %s, 'DELETE')", (t,)).fetchone()[0] is False, t
