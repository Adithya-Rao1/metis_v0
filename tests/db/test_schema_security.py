import uuid
import psycopg
import pytest

H = "a" * 64


def register(c, email="a@x.edu", edu=False, terms="2026-01"):
    return c.execute("SELECT auth_register(%s,%s,%s,%s)", (email, "hash", terms, edu)).fetchone()[0]


def mkproj(c, uid, name="p"):
    return c.execute("INSERT INTO project(owner_id,name) VALUES (%s,%s) RETURNING id", (uid, name)).fetchone()[0]


def mkcomp(c, uid, pid, smi="c1ccccc1"):
    return c.execute("INSERT INTO compound(owner_id,project_id,smiles,canonical_smiles) VALUES (%s,%s,%s,%s) RETURNING id",
                     (uid, pid, smi, smi)).fetchone()[0]


# ---- structure ----
def test_every_user_owned_table_has_owner_and_forced_rls(su):
    rows = su.execute("""
      SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
        EXISTS (SELECT 1 FROM information_schema.columns k WHERE k.table_name=c.relname AND k.column_name IN ('owner_id') )
          OR c.relname='app_user' AS has_owner
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='public' AND c.relkind='r' AND c.relname NOT IN ('model_version','audit_log','deletion_request')""").fetchall()
    assert rows
    for name, rls, force, has_owner in rows:
        assert has_owner, name
        assert rls and force, name


def test_primary_keys_are_uuid_except_audit(su):
    rows = su.execute("""SELECT t.relname, format_type(a.atttypid,a.atttypmod)
      FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(i.indkey)
      JOIN pg_namespace n ON n.oid=t.relnamespace WHERE i.indisprimary AND n.nspname='public' AND t.relname<>'audit_log'""").fetchall()
    assert rows and all(t == "uuid" for _, t in rows)


def test_app_role_flags(su):
    r = su.execute("SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname='metis_app'").fetchone()
    assert r == (False, False, False, False)


def test_app_role_cannot_ddl(app, su):
    uid = register(su)
    c = app.as_user(uid)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("CREATE TABLE evil(x int)")
    c.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("DROP TABLE project")


def test_readonly_role_sees_no_user_content(pg, su):
    with psycopg.connect(pg, autocommit=True) as c:
        c.execute("SET ROLE metis_readonly")
        for t in ("compound", "app_user", "note", "measurement", "prediction", "auth_token", "consent"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f"SELECT * FROM {t}")


# ---- tenant isolation ----
def test_cross_user_isolation_all_tables(app, su):
    a, b = register(su, "a@x.com"), register(su, "b@x.com")
    pa = mkproj(su, a)
    ca = mkcomp(su, a, pa)
    su.execute("INSERT INTO note(owner_id,project_id,body) VALUES (%s,%s,'secret')", (a, pa))
    su.execute("INSERT INTO measurement(owner_id,compound_id,endpoint,value) VALUES (%s,%s,'sol',1)", (a, ca))
    su.execute("INSERT INTO consent(owner_id,scope) VALUES (%s,'train_models')", (a,))
    cb = app.as_user(b)
    for t in ("project", "compound", "note", "measurement", "consent", "terms_acceptance", "app_user"):
        n = cb.execute(f"SELECT count(*) FROM {t} WHERE " + ("id=%s" if t == "app_user" else "owner_id=%s"), (a,)).fetchone()[0]
        assert n == 0, t
    assert cb.execute("SELECT count(*) FROM project").fetchone()[0] == 0
    # cannot update or delete another user's rows
    assert cb.execute("UPDATE project SET name='x' WHERE id=%s", (pa,)).rowcount == 0
    assert cb.execute("DELETE FROM compound WHERE id=%s", (ca,)).rowcount == 0


def test_no_user_context_sees_nothing(app, su):
    a = register(su)
    mkproj(su, a)
    c = app.as_user(None)
    assert c.execute("SELECT count(*) FROM project").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM app_user").fetchone()[0] == 0


def test_cannot_insert_for_another_owner(app, su):
    a, b = register(su, "a@x.com"), register(su, "b@x.com")
    cb = app.as_user(b)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        cb.execute("INSERT INTO project(owner_id,name) VALUES (%s,'steal')", (a,))


def test_cannot_attach_compound_to_other_users_project(su):
    a, b = register(su, "a@x.com"), register(su, "b@x.com")
    pa = mkproj(su, a)
    with pytest.raises(psycopg.errors.CheckViolation):
        mkcomp(su, b, pa)


def test_cannot_change_row_owner(app, su):
    a, b = register(su, "a@x.com"), register(su, "b@x.com")
    pa = mkproj(su, a)
    ca = app.as_user(a)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ca.execute("UPDATE project SET owner_id=%s WHERE id=%s", (b, pa))


# ---- sharing ----
def test_grant_gives_read_only(app, su):
    a, b = register(su, "a@x.com"), register(su, "b@x.com")
    pa = mkproj(su, a)
    mkcomp(su, a, pa)
    cb = app.as_user(b)
    assert cb.execute("SELECT count(*) FROM compound").fetchone()[0] == 0
    cb.rollback()
    su.execute("INSERT INTO grant_(owner_id,project_id,grantee_id,permission) VALUES (%s,%s,%s,'view')", (a, pa, b))
    cb = app.as_user(b)
    assert cb.execute("SELECT count(*) FROM compound").fetchone()[0] == 1
    assert cb.execute("UPDATE compound SET label='x'").rowcount == 0
    assert cb.execute("SELECT count(*) FROM note").fetchone()[0] == 0  # notes are never shared
    su.execute("UPDATE grant_ SET revoked_at=now()")
    cb = app.as_user(b)
    assert cb.execute("SELECT count(*) FROM compound").fetchone()[0] == 0


def test_cannot_grant_to_self(su):
    a = register(su)
    pa = mkproj(su, a)
    with pytest.raises(psycopg.errors.CheckViolation):
        su.execute("INSERT INTO grant_(owner_id,project_id,grantee_id,permission) VALUES (%s,%s,%s,'view')", (a, pa, a))


# ---- sign-up, terms, auth functions ----
def test_register_records_all_three_terms(app, su):
    a = register(su, terms="2026-01")
    rows = su.execute("SELECT document,version FROM terms_acceptance WHERE owner_id=%s ORDER BY document", (a,)).fetchall()
    assert rows == [("disclaimer", "2026-01"), ("privacy", "2026-01"), ("terms", "2026-01")]


def test_register_without_terms_fails_and_creates_no_user(su):
    for bad in ("", None):
        with pytest.raises(psycopg.errors.CheckViolation):
            su.execute("SELECT auth_register('n@x.com','h',%s,false)", (bad,))
    assert su.execute("SELECT count(*) FROM app_user").fetchone()[0] == 0


def test_email_unique_case_insensitive(su):
    register(su, "Dup@X.com")
    with pytest.raises(psycopg.errors.UniqueViolation):
        register(su, "dup@x.com")


def test_pre_auth_functions_work_for_app_role_without_user_context(app, su):
    c = app.as_user(None)
    uid = c.execute("SELECT auth_register('new@x.com','h','v1',false)").fetchone()[0]
    c.commit()
    row = c.execute("SELECT * FROM auth_lookup('NEW@x.com')").fetchone()
    assert row[0] == uid and row[2] is False
    assert c.execute("SELECT count(*) FROM app_user").fetchone()[0] == 0  # still cannot read table directly


def test_terms_append_only(app, su):
    a = register(su)
    c = app.as_user(a)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("UPDATE terms_acceptance SET version='x'")
    c.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("DELETE FROM terms_acceptance")


def test_terms_current_view_returns_latest(app, su):
    a = register(su, terms="v1")
    su.execute("INSERT INTO terms_acceptance(owner_id,document,version,accepted_at) VALUES (%s,'terms','v2',now()+interval '1 second')", (a,))
    c = app.as_user(a)
    rows = dict(c.execute("SELECT document,version FROM terms_current").fetchall())
    assert rows["terms"] == "v2" and rows["privacy"] == "v1"


def test_lockout_after_five_failures_and_reset(app, su):
    a = register(su)
    c = app.as_user(None)
    for _ in range(5):
        c.execute("SELECT auth_record_login(%s,false)", (a,))
    c.commit()
    assert su.execute("SELECT locked_until > now() FROM app_user WHERE id=%s", (a,)).fetchone()[0]
    c.execute("SELECT auth_record_login(%s,true)", (a,))
    c.commit()
    assert su.execute("SELECT locked_until, failed_logins FROM app_user WHERE id=%s", (a,)).fetchone() == (None, 0)


def test_token_single_use_expiry_purpose(app, su):
    a = register(su)
    su.execute("INSERT INTO auth_token(owner_id,purpose,token_hash,expires_at) VALUES (%s,'verify_email',%s,now()+interval '1 hour')", (a, H))
    su.execute("INSERT INTO auth_token(owner_id,purpose,token_hash,expires_at) VALUES (%s,'reset_password',%s,now()-interval '1 hour')", (a, "b" * 64))
    c = app.as_user(None)
    assert c.execute("SELECT auth_consume_token(%s,'reset_password')", (H,)).fetchone()[0] is None  # wrong purpose
    assert c.execute("SELECT auth_consume_token(%s,'reset_password')", ("b" * 64,)).fetchone()[0] is None  # expired
    assert c.execute("SELECT auth_consume_token(%s,'verify_email')", (H,)).fetchone()[0] == a
    assert c.execute("SELECT auth_consume_token(%s,'verify_email')", (H,)).fetchone()[0] is None  # reused
    c.commit()
    assert su.execute("SELECT email_verified_at IS NOT NULL FROM app_user WHERE id=%s", (a,)).fetchone()[0]


def test_token_hash_must_be_64_chars(su):
    a = register(su)
    with pytest.raises(psycopg.errors.CheckViolation):
        su.execute("INSERT INTO auth_token(owner_id,purpose,token_hash,expires_at) VALUES (%s,'verify_email','raw-token',now())", (a,))


# ---- consent ----
def test_consent_default_none_and_one_active_per_scope(su):
    a = register(su)
    assert su.execute("SELECT count(*) FROM consent WHERE owner_id=%s", (a,)).fetchone()[0] == 0
    su.execute("INSERT INTO consent(owner_id,scope) VALUES (%s,'train_models')", (a,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        su.execute("INSERT INTO consent(owner_id,scope) VALUES (%s,'train_models')", (a,))
    su.execute("UPDATE consent SET revoked_at=now()+interval '1 second' WHERE owner_id=%s", (a,))
    su.execute("INSERT INTO consent(owner_id,scope) VALUES (%s,'train_models')", (a,))  # re-grant allowed after revoke


# ---- constraints ----
def test_compound_cap_10(su):
    a = register(su)
    p = mkproj(su, a)
    for i in range(10):
        mkcomp(su, a, p, "C" * (i + 1))
    with pytest.raises(psycopg.errors.CheckViolation):
        mkcomp(su, a, p, "CCCCCCCCCCCC")


def test_smiles_length_cap(su):
    a = register(su)
    p = mkproj(su, a)
    with pytest.raises(psycopg.errors.CheckViolation):
        mkcomp(su, a, p, "C" * 2001)


def test_prediction_requires_interval_and_valid_n(su):
    a = register(su)
    p = mkproj(su, a)
    c = mkcomp(su, a, p)
    mv = su.execute("INSERT INTO model_version(endpoint,version) VALUES ('sol','1') RETURNING id").fetchone()[0]
    ins = "INSERT INTO prediction(owner_id,compound_id,endpoint,model_version_id,ensemble_n,kind,value,interval_low,interval_high,coverage,sample_size,method_note,uncertainty_breakdown) VALUES (%s,%s,'sol',%s,%s,'regression',1,%s,%s,0.9,100,'split conformal','{}')"
    su.execute(ins, (a, c, mv, 5, 0.5, 1.5))
    with pytest.raises(psycopg.errors.CheckViolation):
        su.execute(ins, (a, c, mv, 7, 0.5, 1.5))      # N=7 not offered
    with pytest.raises(psycopg.errors.CheckViolation):
        su.execute(ins, (a, c, mv, 5, None, None))    # missing interval
    with pytest.raises(psycopg.errors.CheckViolation):
        su.execute(ins, (a, c, mv, 5, 2.0, 1.0))      # inverted


def test_model_publish_gate(su):
    ok = dict(d='[{"name":"x"}]', lic=True, lim="limits", split="scaffold", bench='{"a":1}', cov='{"5":0.9}', sha="s")
    q = "INSERT INTO model_version(endpoint,version,status,datasets,licenses_cleared,limitations,split_type,benchmark,coverage_by_n,weights_sha256) VALUES ('e',%s,'published',%s,%s,%s,%s,%s,%s,%s)"
    su.execute(q, ("1", ok["d"], True, "l", "scaffold", ok["bench"], ok["cov"], "s"))
    for i, args in enumerate([
        ("[]", True, "l", "s", "{}", "{}", "s"),                  # no datasets
        (ok["d"], False, "l", "s", "{}", "{}", "s"),              # license not cleared
        (ok["d"], True, "", "s", "{}", "{}", "s"),                # empty limitations
        (ok["d"], True, "l", None, "{}", "{}", "s"),              # no split
        (ok["d"], True, "l", "s", None, "{}", "s"),               # no benchmark
        (ok["d"], True, "l", "s", "{}", None, "s"),               # no coverage
        (ok["d"], True, "l", "s", "{}", "{}", None),              # no weights hash
    ]):
        with pytest.raises(psycopg.errors.CheckViolation):
            su.execute(q, (f"bad{i}",) + args)


# ---- audit ----
def test_audit_log_immutable_even_for_superuser_triggers(app, su):
    a = register(su)
    c = app.as_user(a)
    c.execute("INSERT INTO audit_log(actor_id,action) VALUES (%s,'export')", (a,))
    c.commit()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("UPDATE audit_log SET action='x'")
    c.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("DELETE FROM audit_log")
    c.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        su.execute("DELETE FROM audit_log")  # trigger blocks superuser too


# ---- deletion ----
def test_account_delete_cascades_everything(su):
    a = register(su)
    p = mkproj(su, a)
    c = mkcomp(su, a, p)
    su.execute("INSERT INTO note(owner_id,project_id,body) VALUES (%s,%s,'n')", (a, p))
    su.execute("INSERT INTO measurement(owner_id,compound_id,endpoint,value) VALUES (%s,%s,'sol',1)", (a, c))
    su.execute("INSERT INTO consent(owner_id,scope) VALUES (%s,'analytics')", (a,))
    su.execute("INSERT INTO auth_token(owner_id,purpose,token_hash,expires_at) VALUES (%s,'verify_email',%s,now())", (a, H))
    su.execute("DELETE FROM app_user WHERE id=%s", (a,))
    for t in ("project", "compound", "note", "measurement", "consent", "auth_token", "terms_acceptance"):
        assert su.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0, t


def test_project_delete_cascades_children(su):
    a = register(su)
    p = mkproj(su, a)
    c = mkcomp(su, a, p)
    su.execute("INSERT INTO measurement(owner_id,compound_id,endpoint,value) VALUES (%s,%s,'sol',1)", (a, c))
    su.execute("DELETE FROM project WHERE id=%s", (p,))
    assert su.execute("SELECT count(*) FROM compound").fetchone()[0] == 0
    assert su.execute("SELECT count(*) FROM measurement").fetchone()[0] == 0


def test_deletion_request_has_30_day_backup_purge_and_survives_user(su):
    a = register(su)
    su.execute("INSERT INTO deletion_request(owner_id,scope) VALUES (%s,'account')", (a,))
    su.execute("DELETE FROM app_user WHERE id=%s", (a,))
    r = su.execute("SELECT backups_purge_due - requested_at FROM deletion_request").fetchone()[0]
    assert r.days == 30


def test_app_role_cannot_escalate_tier_or_forge_users(app, su):
    a = register(su)
    c = app.as_user(a)
    for sql in ("UPDATE app_user SET tier='paid'", "UPDATE app_user SET is_edu=true",
                "UPDATE app_user SET email_verified_at=now()", "UPDATE app_user SET failed_logins=0, locked_until=NULL"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(sql)
        c.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("INSERT INTO app_user(email_lower,password_hash) VALUES ('x@y.com','h')")
    c.rollback()
    assert c.execute("UPDATE app_user SET password_hash='new'").rowcount == 1  # own password change allowed
