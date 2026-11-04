"""Red-team tests for the Wave 0 schema and contracts (Agent 9).

Convention: a normal test proves a protection HOLDS. An xfail(strict=True) test asserts the SECURE
behaviour and currently FAILS because of a real bug; when the bug is fixed the xfail flips to XPASS and
strict mode forces the marker to be removed. Finding ids (F-nn) map to private/qa_wave0.md.
"""
import json
import threading
import uuid

import psycopg
import pytest
from psycopg import errors as E

H = "a" * 64
bug = lambda fid, why: pytest.mark.xfail(strict=True, reason=f"{fid}: {why}")


# ---------------- helpers (superuser seeds data; attackers use appc) ----------------
def reg(c, email, edu=False):
    return c.execute("SELECT auth_register(%s,'$2b$04$hhhhhhhhhhhhhhhhhhhh','2026-01')", (email,)).fetchone()[0]


def proj(c, uid, name="p"):
    return c.execute("INSERT INTO project(owner_id,name) VALUES (%s,%s) RETURNING id", (uid, name)).fetchone()[0]


def comp(c, uid, pid, smi="CCO"):
    return c.execute("INSERT INTO compound(owner_id,project_id,smiles,canonical_smiles) VALUES (%s,%s,%s,%s) RETURNING id",
                     (uid, pid, smi, smi)).fetchone()[0]


def grant(c, owner, pid, grantee, perm="view"):
    return c.execute("INSERT INTO grant_(owner_id,project_id,grantee_id,permission) VALUES (%s,%s,%s,%s) RETURNING id",
                     (owner, pid, grantee, perm)).fetchone()[0]


def model(c, status="draft"):
    return c.execute("INSERT INTO model_version(endpoint,version,status) VALUES ('sol',%s,%s) RETURNING id",
                     (str(uuid.uuid4())[:8], status)).fetchone()[0]


def pred_sql(owner, cid, mv, **o):
    d = dict(endpoint="sol", ensemble_n=5, kind="regression", value=1.0, interval_low=0.5, interval_high=1.5,
             probability=None, coverage=0.9, sample_size=10, method_note="m", uncertainty_breakdown='{"ensemble_disagreement":0.5,"similarity_to_training":0.3,"assay_noise":0.2}')
    d.update(o)
    return ("INSERT INTO prediction(owner_id,compound_id,model_version_id,endpoint,ensemble_n,kind,value,interval_low,interval_high,"
            "probability,coverage,sample_size,method_note,uncertainty_breakdown) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (owner, cid, mv, d["endpoint"], d["ensemble_n"], d["kind"], d["value"], d["interval_low"], d["interval_high"],
             d["probability"], d["coverage"], d["sample_size"], d["method_note"], d["uncertainty_breakdown"]))


def rejected(c, sql, params=()):
    """True if the statement is rejected by the database (any DB error)."""
    try:
        c.execute(sql, params)
        return False
    except psycopg.Error:
        return True


@pytest.fixture
def world(su):
    """victim V with project/compound/note/measurement/prediction; attacker M; second account M2."""
    v, m, m2 = reg(su, "v@x.com"), reg(su, "m@x.com"), reg(su, "m2@x.com")
    pv = proj(su, v, "secret-series")
    cv = comp(su, v, pv, "CC(=O)Oc1ccccc1C(=O)O")
    mv = model(su)
    su.execute("INSERT INTO note(owner_id,project_id,body) VALUES (%s,%s,'unpublished idea')", (v, pv))
    su.execute("INSERT INTO measurement(owner_id,compound_id,endpoint,value) VALUES (%s,%s,'sol',2.5)", (v, cv))
    su.execute(*pred_sql(v, cv, mv))
    su.execute("INSERT INTO consent(owner_id,scope) VALUES (%s,'train_models')", (v,))
    return dict(v=v, m=m, m2=m2, pv=pv, cv=cv, mv=mv)


# =====================================================================================
# 1. GRANTS: privilege escalation and shared-read policies
# =====================================================================================
def test_grant_on_project_not_owned_is_rejected(appc, world):
    w = world
    c = appc(w["m"])
    assert rejected(c, "INSERT INTO grant_(owner_id,project_id,grantee_id,permission) VALUES (%s,%s,%s,'view')",
                    (w["m"], w["pv"], w["m2"]))


def test_forged_grant_does_not_expose_victim_data(appc, world):
    w = world
    try:
        appc(w["m"]).execute("INSERT INTO grant_(owner_id,project_id,grantee_id,permission) VALUES (%s,%s,%s,'view')",
                             (w["m"], w["pv"], w["m2"]))
    except psycopg.Error:
        pass
    acc = appc(w["m2"])
    assert acc.execute("SELECT count(*) FROM project").fetchone()[0] == 0
    assert acc.execute("SELECT count(*) FROM compound").fetchone()[0] == 0


def test_owner_can_revoke_any_grant_exposing_his_project(su, appc, world):
    w = world
    # forged grants can no longer exist (composite FK to the owner's project); see test_grant_on_project_not_owned_is_rejected
    assert rejected(su, "INSERT INTO grant_(owner_id,project_id,grantee_id,permission) VALUES (%s,%s,%s,'view')", (w["m"], w["pv"], w["m2"]))


def test_grantee_cannot_regrant(su, appc, world):
    w = world
    grant(su, w["v"], w["pv"], w["m"])
    assert rejected(appc(w["m"]), "INSERT INTO grant_(owner_id,project_id,grantee_id,permission) VALUES (%s,%s,%s,'view')",
                    (w["m"], w["pv"], w["m2"]))


def test_grant_squatting_does_not_block_owner(su, appc, world):
    w = world
    assert rejected(su, "INSERT INTO grant_(owner_id,project_id,grantee_id,permission) VALUES (%s,%s,%s,'view')", (w["m"], w["pv"], w["m2"]))
    su.rollback() if hasattr(su, "rollback") else None
    appc(w["v"]).execute("INSERT INTO grant_(owner_id,project_id,grantee_id,permission) VALUES (%s,%s,%s,'view')", (w["v"], w["pv"], w["m2"]))


def test_grantee_cannot_modify_revoke_or_delete_grant(su, appc, world):
    w = world
    g = grant(su, w["v"], w["pv"], w["m"], "edit")
    c = appc(w["m"])
    assert c.execute("UPDATE grant_ SET permission='edit', revoked_at=NULL WHERE id=%s", (g,)).rowcount == 0
    assert c.execute("DELETE FROM grant_ WHERE id=%s", (g,)).rowcount == 0
    assert su.execute("SELECT count(*) FROM grant_").fetchone()[0] == 1


def test_non_grantee_cannot_see_grants(su, appc, world):
    w = world
    grant(su, w["v"], w["pv"], w["m"])
    assert appc(w["m2"]).execute("SELECT count(*) FROM grant_").fetchone()[0] == 0


def test_grantee_reads_only_project_and_compound(su, appc, world):
    w = world
    grant(su, w["v"], w["pv"], w["m"], "edit")
    c = appc(w["m"])
    assert c.execute("SELECT count(*) FROM project").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM compound").fetchone()[0] == 1
    for t in ("note", "measurement", "prediction", "consent", "auth_token"):
        assert c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0, t
    assert c.execute("SELECT count(*) FROM app_user WHERE id=%s", (w["v"],)).fetchone()[0] == 0


def test_grantee_cannot_write_shared_rows(su, appc, world):
    w = world
    grant(su, w["v"], w["pv"], w["m"], "edit")
    c = appc(w["m"])
    assert c.execute("UPDATE project SET name='pwn' WHERE id=%s", (w["pv"],)).rowcount == 0
    assert c.execute("UPDATE compound SET label='pwn' WHERE id=%s", (w["cv"],)).rowcount == 0
    assert c.execute("DELETE FROM compound WHERE id=%s", (w["cv"],)).rowcount == 0
    assert c.execute("DELETE FROM project WHERE id=%s", (w["pv"],)).rowcount == 0
    # inserting a compound into the shared project is blocked by the owner-match trigger
    assert rejected(c, "INSERT INTO compound(owner_id,project_id,smiles,canonical_smiles) VALUES (%s,%s,'C','C')", (w["m"], w["pv"]))


def test_revoked_grant_ends_access_immediately(su, appc, world):
    w = world
    g = grant(su, w["v"], w["pv"], w["m"])
    c = appc(w["m"])
    assert c.execute("SELECT count(*) FROM project").fetchone()[0] == 1
    su.execute("UPDATE grant_ SET revoked_at=now() WHERE id=%s", (g,))
    assert c.execute("SELECT count(*) FROM project").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM compound").fetchone()[0] == 0


def test_grantee_cannot_infer_via_select_for_update_or_returning(su, appc, world):
    w = world
    grant(su, w["v"], w["pv"], w["m"])
    c = appc(w["m"])
    # row locks need UPDATE policy rows: shared rows must be neither lockable nor returned by writes
    assert c.execute("SELECT id FROM compound FOR UPDATE").fetchall() == []
    assert c.execute("SELECT id FROM project FOR SHARE").fetchall() == []
    assert c.execute("UPDATE project SET name=name WHERE id=%s RETURNING name", (w["pv"],)).fetchall() == []


# =====================================================================================
# 2. Cross-tenant attachment, triggers, cap
# =====================================================================================
def test_compound_cannot_be_attached_to_foreign_project(appc, world):
    w = world
    assert rejected(appc(w["m"]), "INSERT INTO compound(owner_id,project_id,smiles,canonical_smiles) VALUES (%s,%s,'C','C')",
                    (w["m"], w["pv"]))


def test_compound_update_cannot_move_into_foreign_project(su, appc, world):
    w = world
    pm = proj(su, w["m"])
    cm = comp(su, w["m"], pm)
    assert rejected(appc(w["m"]), "UPDATE compound SET project_id=%s WHERE id=%s", (w["pv"], cm))


def test_measurement_on_foreign_compound_rejected(appc, world):
    w = world
    assert rejected(appc(w["m"]), "INSERT INTO measurement(owner_id,compound_id,endpoint,value) VALUES (%s,%s,'sol',9)", (w["m"], w["cv"]))


def test_prediction_on_foreign_compound_rejected(appc, world):
    w = world
    assert rejected(appc(w["m"]), *pred_sql(w["m"], w["cv"], w["mv"]))


def test_note_on_foreign_project_rejected(appc, world):
    w = world
    assert rejected(appc(w["m"]), "INSERT INTO note(owner_id,project_id,body) VALUES (%s,%s,'x')", (w["m"], w["pv"]))


def test_fk_error_distinguishes_missing_vs_existing_parent_is_only_uuid_oracle(appc, world):
    """INFO: a random UUID fails with FK violation; guessable only with the 128-bit id."""
    w = world
    with pytest.raises(E.ForeignKeyViolation):
        appc(w["m"]).execute("INSERT INTO note(owner_id,project_id,body) VALUES (%s,%s,'x')", (w["m"], uuid.uuid4()))


def test_owner_cannot_be_changed_by_update(su, appc, world):
    w = world
    pm = proj(su, w["m"])
    assert rejected(appc(w["m"]), "UPDATE project SET owner_id=%s WHERE id=%s", (w["v"], pm))
    assert appc(w["m"]).execute("UPDATE project SET owner_id=%s WHERE id=%s", (w["m"], w["pv"])).rowcount == 0  # hijack
    assert su.execute("SELECT owner_id FROM project WHERE id=%s", (w["pv"],)).fetchone()[0] == w["v"]


def test_cap_cannot_be_bypassed_by_moving_compounds(su, appc, world):
    w = world
    full, spare = proj(su, w["m"], "full"), proj(su, w["m"], "spare")
    for _ in range(10):
        comp(su, w["m"], full)
    c1 = comp(su, w["m"], spare)
    assert rejected(appc(w["m"]), "UPDATE compound SET project_id=%s WHERE id=%s", (full, c1))


def test_cap_holds_under_concurrent_inserts(su, appc, world):
    w = world
    p = proj(su, w["m"])
    conns = [appc(w["m"]) for _ in range(14)]
    ok, errs = [], []

    def go(c):
        try:
            c.execute("INSERT INTO compound(owner_id,project_id,smiles,canonical_smiles) VALUES (%s,%s,'C','C')", (w["m"], p))
            ok.append(1)
        except psycopg.Error as e:
            errs.append(e)

    ts = [threading.Thread(target=go, args=(c,)) for c in conns]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert su.execute("SELECT count(*) FROM compound WHERE project_id=%s", (p,)).fetchone()[0] == 10


def test_cap_not_bypassable_by_temp_table_shadowing(su, appc, world):
    c = appc(world["m"])
    with pytest.raises(E.InsufficientPrivilege):
        c.execute("CREATE TEMP TABLE compound (project_id uuid)")


def test_owner_trigger_not_bypassable_by_temp_project(appc, world):
    c = appc(world["m"])
    with pytest.raises(E.InsufficientPrivilege):
        c.execute("CREATE TEMP TABLE project (id uuid, owner_id uuid)")


def test_app_role_has_no_temp_privilege(su):
    assert su.execute("SELECT has_database_privilege('metis_app', current_database(), 'TEMP')").fetchone()[0] is False


@bug("F-08 LOW", "global advisory lock keyed on a project id: anyone holding it can stall inserts into that project")
def test_advisory_lock_cannot_stall_other_tenants(pg, su, appc, world):
    w = world
    holder = appc(w["m"])
    holder.execute("BEGIN")
    holder.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (str(w["pv"]),))
    c = appc(w["v"])
    c.execute("SET lock_timeout='500ms'")
    try:
        c.execute("INSERT INTO compound(owner_id,project_id,smiles,canonical_smiles) VALUES (%s,%s,'C','C')", (w["v"], w["pv"]))
    finally:
        holder.execute("ROLLBACK")


# =====================================================================================
# 3. Tables without RLS
# =====================================================================================
def test_audit_log_not_readable_across_users(appc, world):
    w = world
    rows = appc(w["m"]).execute("SELECT count(*) FROM audit_log WHERE actor_id=%s", (w["v"],)).fetchone()[0]
    assert rows == 0


def test_audit_log_cannot_be_forged_for_other_actor(appc, world):
    w = world
    assert rejected(appc(w["m"]), "INSERT INTO audit_log(actor_id,action) VALUES (%s,'login_ok')", (w["v"],))


def test_audit_detail_is_bounded(appc, world):
    w = world
    assert rejected(appc(w["m"]), "INSERT INTO audit_log(actor_id,action,detail) VALUES (%s,'x',to_jsonb(repeat('A',3000000)))", (w["m"],))


def test_deletion_request_not_readable_across_users(su, appc, world):
    w = world
    su.execute("INSERT INTO deletion_request(owner_id,scope) VALUES (%s,'account')", (w["v"],))
    assert appc(w["m"]).execute("SELECT count(*) FROM deletion_request").fetchone()[0] == 0


def test_cannot_forge_deletion_request_for_other_user(appc, world):
    w = world
    assert rejected(appc(w["m"]), "INSERT INTO deletion_request(owner_id,scope) VALUES (%s,'account')", (w["v"],))


def test_cannot_tamper_with_deletion_requests(su, appc, world):
    w = world
    su.execute("INSERT INTO deletion_request(owner_id,scope) VALUES (%s,'account')", (w["v"],))
    c = appc(w["m"])
    with pytest.raises(E.InsufficientPrivilege):
        c.execute("UPDATE deletion_request SET rows_purged_at=now()")
    c.rollback()
    with pytest.raises(E.InsufficientPrivilege):
        c.execute("DELETE FROM deletion_request")


def test_draft_models_hidden_from_app_role(su, appc, world):
    w = world
    assert appc(w["m"]).execute("SELECT count(*) FROM model_version WHERE status='draft'").fetchone()[0] == 0


@bug("F-11 LOW", "predictions may reference draft/retired models (publish gate not enforced on use)")
def test_prediction_requires_published_model(appc, su, world):
    w = world
    pm = proj(su, w["m"])
    cm = comp(su, w["m"], pm)
    assert rejected(appc(w["m"]), *pred_sql(w["m"], cm, w["mv"]))


def test_app_cannot_write_model_version(appc, world):
    c = appc(world["m"])
    assert rejected(c, "INSERT INTO model_version(endpoint,version) VALUES ('x','y')")
    assert rejected(c, "UPDATE model_version SET status='published'")
    assert rejected(c, "DELETE FROM model_version")


def test_readonly_role_surface(su, pg, world):
    tabs = su.execute("""SELECT table_name, privilege_type FROM information_schema.role_table_grants
                         WHERE grantee='metis_readonly' AND table_schema='public'""").fetchall()
    assert set(tabs) <= {("model_version", "SELECT"), ("deletion_request", "SELECT")}
    c = psycopg.connect(pg, autocommit=True)
    c.execute("SET ROLE metis_readonly")
    for f in ("auth_lookup('v@x.com')", "auth_record_login(gen_random_uuid(), false)", "auth_consume_token('%s','verify_email')" % H,
              "auth_register('z@x.com','$2b$04$hhhhhhhhhhhhhhhhhhhh','1')"):
        with pytest.raises(E.InsufficientPrivilege):
            c.execute(f"SELECT {f}")
    for t in ("audit_log", "terms_current", "grant_"):
        with pytest.raises(E.InsufficientPrivilege):
            c.execute(f"SELECT * FROM {t}")
    c.close()


def test_no_public_table_or_sequence_privileges(su):
    assert su.execute("SELECT count(*) FROM information_schema.role_table_grants WHERE grantee='PUBLIC' AND table_schema='public'").fetchone()[0] == 0
    assert su.execute("SELECT count(*) FROM information_schema.usage_privileges WHERE grantee='PUBLIC' AND object_schema='public'").fetchone()[0] == 0


def test_security_definer_functions_not_executable_by_public(su):
    rows = su.execute("""SELECT p.proname, p.proacl IS NULL OR EXISTS (SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee=0)
                         FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                         WHERE n.nspname='public' AND p.prosecdef""").fetchall()
    assert rows and not any(public for _, public in rows)


def test_definer_functions_pin_search_path_and_owner_is_unprivileged(su):
    rows = su.execute("""SELECT p.proname, p.proconfig, r.rolsuper, r.rolcanlogin FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
                         JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef""").fetchall()
    for name, cfg, sup, login in rows:
        assert cfg and any(x.startswith("search_path=") for x in cfg), name
        assert not sup and not login, name


@bug("F-12 LOW", "default privileges not hardened: every future function is EXECUTE-able by PUBLIC; trigger fns already are")
def test_default_function_privileges_revoked_from_public(su):
    n = su.execute("SELECT count(*) FROM pg_default_acl WHERE defaclobjtype='f'").fetchone()[0]
    assert n >= 1


def test_metis_auth_role_is_minimal(su):
    assert su.execute("SELECT rolcanlogin, rolsuper, rolcreaterole FROM pg_roles WHERE rolname='metis_auth'").fetchone() == (False, False, False)
    for t in ("project", "note", "measurement", "prediction", "consent", "grant_", "deletion_request", "model_version"):  # compound SELECT is deliberate (cap trigger)
        assert su.execute("SELECT has_table_privilege('metis_auth', %s, 'SELECT')", (t,)).fetchone()[0] is False, t
    assert su.execute("SELECT has_table_privilege('metis_auth','app_user','DELETE')").fetchone()[0] is False
    assert su.execute("SELECT pg_has_role('metis_app','metis_auth','MEMBER')").fetchone()[0] is False


# =====================================================================================
# 4. Identity (metis.user_id), role escape
# =====================================================================================
def test_missing_blank_or_garbage_identity_fails_closed(pg, su, appc, world):
    for ident in (None, ""):
        c = appc(ident)
        assert c.execute("SELECT count(*) FROM project").fetchone()[0] == 0
        assert rejected(c, "INSERT INTO project(owner_id,name) VALUES (%s,'x')", (world["m"],))
    c = appc(world["v"])
    c.execute("SELECT set_config('metis.user_id','not-a-uuid',false)")
    with pytest.raises(E.InvalidTextRepresentation):
        c.execute("SELECT count(*) FROM project")
    c.execute("SELECT set_config('metis.user_id',' ',false)")
    with pytest.raises(E.InvalidTextRepresentation):
        c.execute("SELECT count(*) FROM project")


@bug("F-13 INFO (accepted trust boundary)", "metis_app can set_config('metis.user_id', <any uuid>) and become that user; API must never expose raw SQL / must set it only via SET LOCAL after auth")
def test_app_role_cannot_impersonate_by_setting_guc(appc, world):
    w = world
    c = appc(w["m"])
    c.execute("SELECT set_config('metis.user_id', %s, false)", (str(w["v"]),))
    assert c.execute("SELECT count(*) FROM project").fetchone()[0] == 0


def test_set_local_identity_does_not_leak_to_next_transaction(appc, world):
    c = appc(world["v"])
    c.execute("SELECT set_config('metis.user_id','',false)")
    c.execute("BEGIN")
    c.execute("SELECT set_config('metis.user_id', %s, true)", (str(world["v"]),))
    assert c.execute("SELECT count(*) FROM project").fetchone()[0] == 1
    c.execute("COMMIT")
    assert c.execute("SELECT count(*) FROM project").fetchone()[0] == 0


def test_session_level_identity_leaks_across_pooled_requests(appc, world):
    """Documents the pool hazard: session-scope identity survives COMMIT/ROLLBACK, so the API MUST use SET LOCAL."""
    c = appc(world["v"])
    c.execute("BEGIN")
    c.execute("ROLLBACK")
    assert c.execute("SELECT count(*) FROM project").fetchone()[0] == 1


def test_login_role_cannot_escape_to_privileged_roles(login_role, world):
    c = login_role(world["m"])
    for stmt in ("SET ROLE metis_auth", "SET ROLE postgres", "SET ROLE metis_readonly", "SET SESSION AUTHORIZATION postgres"):
        with pytest.raises(E.InsufficientPrivilege):
            c.execute(stmt)
    c.execute("RESET ROLE")
    assert c.execute("SELECT current_user, session_user").fetchone() == ("rt_login", "rt_login")
    for stmt in ("CREATE TABLE public.evil(x int)", "CREATE FUNCTION public.f() RETURNS int LANGUAGE sql AS 'select 1'",
                 "CREATE SCHEMA evil", "ALTER SYSTEM SET work_mem='1GB'", "CREATE ROLE evil", "CREATE EXTENSION dblink",
                 "SELECT pg_read_file('/etc/passwd')", "COPY (SELECT 1) TO PROGRAM 'id'", "SELECT lo_import('/etc/passwd')",
                 "ALTER TABLE project DISABLE ROW LEVEL SECURITY", "ALTER TABLE project NO FORCE ROW LEVEL SECURITY",
                 "DROP POLICY project_owner ON project", "TRUNCATE project", "TRUNCATE audit_log", "ALTER TABLE project OWNER TO rt_login",
                 "ALTER FUNCTION auth_lookup(text) OWNER TO rt_login", "CREATE OR REPLACE FUNCTION public.current_user_id() RETURNS uuid LANGUAGE sql AS 'select null::uuid'",
                 "DROP FUNCTION current_user_id()"):
        with pytest.raises(psycopg.Error):
            c.execute(stmt)


def test_app_cannot_bypass_rls_via_session_replication_or_row_security(login_role, world):
    c = login_role(world["m"])
    with pytest.raises(E.InsufficientPrivilege):
        c.execute("SET session_replication_role = replica")
    c.execute("SET row_security = off")  # for non-owner, off means ERROR on access rather than bypass
    with pytest.raises(psycopg.Error):
        c.execute("SELECT * FROM project")


def test_current_user_id_is_not_security_definer_and_spoof_via_view_function_impossible(su):
    assert su.execute("SELECT prosecdef FROM pg_proc WHERE proname='current_user_id'").fetchone()[0] is False


# =====================================================================================
# 5. SECURITY DEFINER abuse
# =====================================================================================
@bug("F-14 MEDIUM", "auth_lookup(any email) returns uuid+bcrypt hash to any app-role caller: with SQL exec you can harvest hashes & uuids (uuids unlock F-01/F-04/F-10)")
def test_auth_lookup_does_not_return_hash_to_unauthenticated_caller(appc, world):
    row = appc(None).execute("SELECT * FROM auth_lookup('v@x.com')").fetchone()
    assert row is None or row[1] is None


@bug("F-15 MEDIUM", "auth_record_login trusts p_user: any caller can lock any account out (5 calls)")
def test_cannot_lock_out_another_user(su, appc, world):
    w = world
    c = appc(w["m"])
    for _ in range(5):
        c.execute("SELECT auth_record_login(%s,false)", (w["v"],))
    assert su.execute("SELECT locked_until FROM app_user WHERE id=%s", (w["v"],)).fetchone()[0] is None


def test_success_record_does_not_clear_active_lock(su, appc, world):
    w = world
    for _ in range(5):
        su.execute("SELECT auth_record_login(%s,false)", (w["v"],))
    appc(None).execute("SELECT auth_record_login(%s,true)", (w["v"],))
    assert su.execute("SELECT locked_until > now() FROM app_user WHERE id=%s", (w["v"],)).fetchone()[0] is True


def test_lock_not_extended_by_failures_while_locked(su, world):
    v = world["v"]
    for _ in range(5):
        su.execute("SELECT auth_record_login(%s,false)", (v,))
    first = su.execute("SELECT locked_until FROM app_user WHERE id=%s", (v,)).fetchone()[0]
    su.execute("SELECT pg_sleep(0.05)")
    su.execute("SELECT auth_record_login(%s,false)", (v,))
    assert su.execute("SELECT locked_until FROM app_user WHERE id=%s", (v,)).fetchone()[0] == first


def test_record_login_does_not_audit_unknown_user(su, appc):
    appc(None).execute("SELECT auth_record_login(%s,true)", (uuid.uuid4(),))
    assert su.execute("SELECT count(*) FROM audit_log WHERE action='login_ok'").fetchone()[0] == 0


def test_user_cannot_self_issue_verification_token(su, appc, world):
    w = world
    c = appc(w["m"])
    try:
        c.execute("INSERT INTO auth_token(owner_id,purpose,token_hash,expires_at) VALUES (%s,'verify_email',%s,now()+interval '1 day')", (w["m"], H))
        c.execute("SELECT auth_consume_token(%s,'verify_email')", (H,))
    except psycopg.Error:
        pass
    assert su.execute("SELECT email_verified_at FROM app_user WHERE id=%s", (w["m"],)).fetchone()[0] is None


def test_consumed_token_cannot_be_resurrected(su, appc, world):
    w = world
    su.execute("INSERT INTO auth_token(owner_id,purpose,token_hash,expires_at,used_at) VALUES (%s,'reset_password',%s,now()+interval '1 day',now())", (w["m"], H))
    assert rejected(appc(w["m"]), "UPDATE auth_token SET used_at=NULL WHERE token_hash=%s", (H,))


def test_token_consumption_rules(su, appc, world):
    w = world
    h2, h3 = "b" * 64, "c" * 64
    su.execute("INSERT INTO auth_token(owner_id,purpose,token_hash,expires_at) VALUES (%s,'reset_password',%s,now()+interval '1 hour')", (w["v"], H))
    su.execute("INSERT INTO auth_token(owner_id,purpose,token_hash,expires_at) VALUES (%s,'verify_email',%s,now()-interval '1 hour')", (w["v"], h2))
    c = appc(w["m"])  # someone else's session: knowing the hash is the only credential
    assert c.execute("SELECT auth_consume_token(%s,'verify_email')", (H,)).fetchone()[0] is None   # wrong purpose
    assert c.execute("SELECT auth_consume_token(%s,'reset_password')", (h2,)).fetchone()[0] is None  # wrong purpose+expired
    assert c.execute("SELECT auth_consume_token(%s,'verify_email')", (h2,)).fetchone()[0] is None   # expired
    assert c.execute("SELECT auth_consume_token(%s,'reset_password')", (H,)).fetchone()[0] == w["v"]
    assert c.execute("SELECT auth_consume_token(%s,'reset_password')", (H,)).fetchone()[0] is None  # replay
    assert c.execute("SELECT auth_consume_token(%s,'reset_password')", (h3,)).fetchone()[0] is None  # unknown
    # SQLi-shaped hash never matches / never errors
    assert c.execute("SELECT auth_consume_token(%s,'reset_password')", ("' OR '1'='1",)).fetchone()[0] is None


def test_token_single_use_under_concurrency(su, appc, world):
    w = world
    su.execute("INSERT INTO auth_token(owner_id,purpose,token_hash,expires_at) VALUES (%s,'reset_password',%s,now()+interval '1 hour')", (w["v"], H))
    conns = [appc(None) for _ in range(8)]
    wins = []

    def go(c):
        if c.execute("SELECT auth_consume_token(%s,'reset_password')", (H,)).fetchone()[0] is not None:
            wins.append(1)

    ts = [threading.Thread(target=go, args=(c,)) for c in conns]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(wins) == 1


def test_temp_table_cannot_shadow_definer_function_tables(appc, world):
    c = appc(None)
    with pytest.raises(E.InsufficientPrivilege):  # TEMP revoked from PUBLIC; functions also pin search_path
        c.execute("CREATE TEMP TABLE app_user (id uuid)")


def test_lookup_ignores_deleted_and_is_exact_match(su, appc, world):
    su.execute("UPDATE app_user SET deleted_at=now() WHERE email_lower='m@x.com'")
    c = appc(None)
    assert c.execute("SELECT count(*) FROM auth_lookup('M@X.COM')").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM auth_lookup('V@X.com')").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM auth_lookup('%')").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM auth_lookup(%s)", ("v@x.com' OR '1'='1",)).fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM auth_lookup(NULL)").fetchone()[0] == 0


def test_register_cannot_self_assert_edu(su, appc):
    uid = appc(None).execute("SELECT auth_register('x@gmail.com','$2b$04$hhhhhhhhhhhhhhhhhhhh','1')").fetchone()[0]
    assert su.execute("SELECT tier FROM app_user WHERE id=%s", (uid,)).fetchone()[0] == "free"


@pytest.mark.parametrize("email,ph", [("abc", "$2b$04$hhhhhhhhhhhhhhhhhhhh"), ("a@x.com", ""), (" a@x.com", "$2b$04$hhhhhhhhhhhhhhhhhhhh"), ("a@x.com ", "$2b$04$hhhhhhhhhhhhhhhhhhhh"), ("a@x.com​", "$2b$04$hhhhhhhhhhhhhhhhhhhh"), ("a\n@x.com", "$2b$04$hhhhhhhhhhhhhhhhhhhh")])
def test_register_rejects_malformed_email_or_empty_hash(su, email, ph):
    assert rejected(su, "SELECT auth_register(%s,%s,'1')", (email, ph))


@bug("F-18 LOW", "no Unicode normalisation (NFKC): fullwidth lookalike registers as a distinct account")
def test_email_lookalike_collision(su):
    reg(su, "a@x.com")
    assert rejected(su, "SELECT auth_register(%s,'$2b$04$hhhhhhhhhhhhhhhhhhhh','1')", ("ａ@x.com",))


def test_register_duplicate_is_atomic_and_leaves_no_partial_rows(su):
    reg(su, "dup@x.com")
    n = su.execute("SELECT count(*) FROM terms_acceptance").fetchone()[0]
    with pytest.raises(E.UniqueViolation):
        reg(su, "DUP@x.com")
    assert su.execute("SELECT count(*) FROM terms_acceptance").fetchone()[0] == n


def test_app_cannot_touch_privileged_user_columns(appc, world):
    w = world
    c = appc(w["m"])
    for col, val in (("tier", "'paid'"), ("is_edu", "true"), ("email_verified_at", "now()"), ("failed_logins", "0"),
                     ("locked_until", "NULL"), ("deleted_at", "NULL"), ("email_lower", "'z@x.com'"), ("id", "gen_random_uuid()")):
        with pytest.raises(E.InsufficientPrivilege):
            c.execute(f"UPDATE app_user SET {col}={val} WHERE id=%s", (w["m"],))
    with pytest.raises(E.InsufficientPrivilege):
        c.execute("INSERT INTO app_user(email_lower,password_hash) VALUES ('q@x.com','$2b$04$hhhhhhhhhhhhhhhhhhhh')")
    assert c.execute("UPDATE app_user SET password_hash='new' WHERE id=%s", (w["v"],)).rowcount == 0  # not another user's


# =====================================================================================
# 6. Terms / consent integrity
# =====================================================================================
def test_cannot_forge_terms_or_consent_for_other_user_or_revoke_theirs(su, appc, world):
    w = world
    c = appc(w["m"])
    assert rejected(c, "INSERT INTO terms_acceptance(owner_id,document,version) VALUES (%s,'terms','2026-01')", (w["v"],))
    assert rejected(c, "INSERT INTO consent(owner_id,scope) VALUES (%s,'share_dataset')", (w["v"],))
    assert c.execute("UPDATE consent SET revoked_at=now() WHERE owner_id=%s", (w["v"],)).rowcount == 0
    c.rollback()
    assert rejected(c, "DELETE FROM consent WHERE owner_id=%s", (w["v"],))  # DELETE privilege revoked entirely
    c.rollback()
    assert su.execute("SELECT count(*) FROM consent WHERE owner_id=%s AND revoked_at IS NULL", (w["v"],)).fetchone()[0] == 1


def test_terms_cannot_be_updated_or_deleted_by_owner(appc, world):
    c = appc(world["v"])
    with pytest.raises(E.InsufficientPrivilege):
        c.execute("UPDATE terms_acceptance SET version='x'")
    with pytest.raises(E.InsufficientPrivilege):
        c.execute("DELETE FROM terms_acceptance")
    with pytest.raises(E.InsufficientPrivilege):
        c.execute("TRUNCATE terms_acceptance")


def test_terms_current_view_is_tenant_scoped(appc, world):
    assert appc(world["m"]).execute("SELECT count(*) FROM terms_current WHERE owner_id=%s", (world["v"],)).fetchone()[0] == 0
    assert appc(world["m"]).execute("SELECT count(*) FROM terms_current").fetchone()[0] == 3


def test_terms_acceptance_time_not_client_controlled(appc, world):
    w = world
    c = appc(w["m"])
    assert rejected(c, "INSERT INTO terms_acceptance(owner_id,document,version,accepted_at) VALUES (%s,'terms','9','2000-01-01')", (w["m"],))


def test_future_dated_terms_cannot_shadow_current(appc, world):
    w = world
    c = appc(w["m"])
    try:
        c.execute("INSERT INTO terms_acceptance(owner_id,document,version,accepted_at) VALUES (%s,'terms','OLD',now()+interval '100 years')", (w["m"],))
    except psycopg.Error:
        pass
    c.execute("INSERT INTO terms_acceptance(owner_id,document,version) VALUES (%s,'terms','NEW')", (w["m"],))
    assert c.execute("SELECT version FROM terms_current WHERE document='terms'").fetchone()[0] == "NEW"


def test_consent_history_not_forgeable(appc, world):
    w = world
    c = appc(w["m"])
    assert rejected(c, "INSERT INTO consent(owner_id,scope,granted_at) VALUES (%s,'analytics','2000-01-01')", (w["m"],)) and \
        rejected(c, "DELETE FROM consent WHERE owner_id=%s", (w["m"],))


# =====================================================================================
# 7. Check constraints
# =====================================================================================
@pytest.mark.parametrize("kw", [dict(value="NaN"), dict(value="Infinity"), dict(interval_low="NaN", interval_high="NaN"),
                                dict(interval_low="-Infinity", interval_high="Infinity"), dict(interval_low=1.0, interval_high="NaN")])
def test_prediction_rejects_non_finite(su, world, kw):
    w = world
    assert rejected(su, *pred_sql(w["v"], w["cv"], w["mv"], **kw))


@pytest.mark.parametrize("val", ["NaN", "Infinity", "-Infinity"])
def test_measurement_rejects_non_finite(su, world, val):
    w = world
    assert rejected(su, "INSERT INTO measurement(owner_id,compound_id,endpoint,value) VALUES (%s,%s,'sol',%s)", (w["v"], w["cv"], val))


def test_prediction_value_inside_interval(su, world):
    w = world
    assert rejected(su, *pred_sql(w["v"], w["cv"], w["mv"], value=100.0, interval_low=0.0, interval_high=1.0))


@pytest.mark.parametrize("kw", [dict(coverage="NaN"), dict(coverage="Infinity"), dict(coverage=1.0), dict(coverage=0), dict(ensemble_n=7),
                                dict(ensemble_n=0), dict(sample_size=0), dict(kind="classification", probability="NaN"),
                                dict(kind="classification", probability=1.0000001), dict(kind="verdict")])
def test_prediction_core_checks_hold(su, world, kw):
    w = world
    assert rejected(su, *pred_sql(w["v"], w["cv"], w["mv"], **kw))


@pytest.mark.parametrize("val", ["{}", "null", '"x"', "[1,2]", '{"ensemble_disagreement":5,"similarity_to_training":5,"assay_noise":5}'])
def test_breakdown_must_be_well_formed(su, world, val):
    w = world
    assert rejected(su, *pred_sql(w["v"], w["cv"], w["mv"], uncertainty_breakdown=val))


@pytest.mark.parametrize("col", ["uncertainty_breakdown", "method_note"])
def test_large_payloads_rejected(su, world, col):
    w = world
    big = json.dumps({"k": "A" * 3_000_000}) if col == "uncertainty_breakdown" else "A" * 3_000_000
    assert rejected(su, *pred_sql(w["v"], w["cv"], w["mv"], **{col: big}))


@pytest.mark.parametrize("sql,params", [
    ("INSERT INTO project(owner_id,name) VALUES ({o},'   ')", ()),
    ("INSERT INTO project(owner_id,name) VALUES ({o},E'\\n\\t')", ()),
    ("INSERT INTO compound(owner_id,project_id,smiles,canonical_smiles) VALUES ({o},{p},'  ','  ')", ()),
    ("INSERT INTO compound(owner_id,project_id,smiles,canonical_smiles) VALUES ({o},{p},E'C\\nC',E'C\\nC')", ()),
    ("INSERT INTO compound(owner_id,project_id,smiles,canonical_smiles) VALUES ({o},{p},E'C\\x01C','C')", ()),
    ("INSERT INTO compound(owner_id,project_id,smiles,canonical_smiles,label) VALUES ({o},{p},'C','C',E'\\x1b[31m')", ()),
    ("INSERT INTO measurement(owner_id,compound_id,endpoint,value) VALUES ({o},{c},' ',1)", ()),
])
def test_text_hygiene(su, world, sql, params):
    w = world
    assert rejected(su, sql.format(o=f"'{w['v']}'", p=f"'{w['pv']}'", c=f"'{w['cv']}'"), params)


def test_method_note_not_blank(su, world):
    w = world
    assert rejected(su, *pred_sql(w["v"], w["cv"], w["mv"], method_note="   "))


def test_nul_byte_text_rejected(su, world):
    with pytest.raises((psycopg.DataError, ValueError)):
        su.execute("INSERT INTO project(owner_id,name) VALUES (%s,%s)", (world["v"], "a\x00b"))


def test_empty_and_overlong_text_rejected(su, world):
    w = world
    assert rejected(su, "INSERT INTO project(owner_id,name) VALUES (%s,'')", (w["v"],))
    assert rejected(su, "INSERT INTO project(owner_id,name) VALUES (%s,%s)", (w["v"], "x" * 201))
    assert rejected(su, "INSERT INTO note(owner_id,project_id,body) VALUES (%s,%s,%s)", (w["v"], w["pv"], "x" * 20001))
    assert rejected(su, "INSERT INTO compound(owner_id,project_id,smiles,canonical_smiles) VALUES (%s,%s,%s,'C')", (w["v"], w["pv"], "C" * 2001))
    assert rejected(su, "INSERT INTO app_user(email_lower,password_hash) VALUES ('A@X.COM','$2b$04$hhhhhhhhhhhhhhhhhhhh')")  # uppercase refused at table level
    assert rejected(su, "INSERT INTO app_user(email_lower,password_hash) VALUES ('ab','$2b$04$hhhhhhhhhhhhhhhhhhhh')")


@pytest.mark.parametrize("kw", [
    "benchmark='null'::jsonb", "coverage_by_n='null'::jsonb", "datasets='[null]'::jsonb", "datasets='[\"\"]'::jsonb",
    "limitations='   '", "weights_sha256='x'", "split_type=''", "benchmark='{}'::jsonb", "coverage_by_n='[]'::jsonb"])
def test_publish_gate_rejects_junk(su, kw):
    base = dict(datasets="'[\"chembl\"]'::jsonb", benchmark="'{\"rmse\":1}'::jsonb", coverage_by_n="'{\"5\":0.9}'::jsonb",
                limitations="'x'", split_type="'scaffold'", weights_sha256="'" + "a" * 64 + "'")
    k, v = kw.split("=", 1)
    base[k] = v
    cols = ",".join(base)
    vals = ",".join(base.values())
    assert rejected(su, f"INSERT INTO model_version(endpoint,version,status,licenses_cleared,{cols}) VALUES ('e','v','published',true,{vals})")


def test_publish_gate_blocks_obviously_incomplete(su):
    assert rejected(su, "INSERT INTO model_version(endpoint,version,status) VALUES ('e','v','published')")
    assert rejected(su, "INSERT INTO model_version(endpoint,version,status,licenses_cleared,datasets) VALUES ('e','v','published',true,'{}')")
    mv = model(su)
    assert rejected(su, "UPDATE model_version SET status='published' WHERE id=%s", (mv,))


# =====================================================================================
# 8. Cascade completeness
# =====================================================================================
def test_user_deletion_leaves_no_rows_anywhere(su, appc, world):
    w = world
    m = w["m"]
    pm = proj(su, m)
    cm = comp(su, m, pm)
    su.execute("INSERT INTO note(owner_id,project_id,body) VALUES (%s,%s,'n')", (m, pm))
    su.execute("INSERT INTO measurement(owner_id,compound_id,endpoint,value) VALUES (%s,%s,'sol',1)", (m, cm))
    su.execute(*pred_sql(m, cm, w["mv"]))
    su.execute("INSERT INTO consent(owner_id,scope) VALUES (%s,'analytics')", (m,))
    su.execute("INSERT INTO auth_token(owner_id,purpose,token_hash,expires_at) VALUES (%s,'reset_password',%s,now()+interval '1 day')", (m, "d" * 64))
    grant(su, m, pm, w["m2"])
    grant(su, w["v"], w["pv"], m)  # m as grantee elsewhere
    assert appc(m).execute("DELETE FROM app_user WHERE id=%s", (m,)).rowcount == 1  # app role may delete itself
    cols = su.execute("""SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public'
                         AND column_name IN ('owner_id','grantee_id') AND table_name NOT IN ('deletion_request')""").fetchall()
    assert len(cols) >= 9
    for t, col in cols:
        assert su.execute(f'SELECT count(*) FROM "{t}" WHERE "{col}"=%s', (m,)).fetchone()[0] == 0, (t, col)
    assert su.execute("SELECT count(*) FROM app_user WHERE id=%s", (m,)).fetchone()[0] == 0
    assert su.execute("SELECT count(*) FROM project WHERE id=%s", (w["pv"],)).fetchone()[0] == 1  # victim untouched
    assert su.execute("SELECT count(*) FROM grant_ WHERE grantee_id=%s", (m,)).fetchone()[0] == 0


@bug("F-24 LOW", "audit_log keeps actor_id and 'app_user:<uuid>' resource after account deletion (no pseudonymisation path; trigger even blocks it)")
def test_audit_log_has_no_residue_after_account_deletion(su, world):
    w = world
    su.execute("DELETE FROM app_user WHERE id=%s", (w["m"],))
    assert su.execute("SELECT count(*) FROM audit_log WHERE actor_id=%s OR resource LIKE %s", (w["m"], f"%{w['m']}%")).fetchone()[0] == 0


def test_project_delete_removes_children_and_nothing_else(su, appc, world):
    w = world
    appc(w["v"]).execute("DELETE FROM project WHERE id=%s", (w["pv"],))
    for t in ("compound", "note", "measurement", "prediction"):
        assert su.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0, t
    assert su.execute("SELECT count(*) FROM app_user").fetchone()[0] == 3


def test_model_version_with_predictions_cannot_be_deleted(su, world):
    assert rejected(su, "DELETE FROM model_version WHERE id=%s", (world["mv"],))


@bug("F-25 LOW", "identity is just a GUC: a deleted user's session keeps working where no FK exists (audit_log has none)")
def test_deleted_users_session_is_dead(appc, world):
    w = world
    c = appc(w["m"])
    c.execute("DELETE FROM app_user WHERE id=%s", (w["m"],))
    assert rejected(c, "INSERT INTO audit_log(actor_id,action) VALUES (%s,'ghost')", (w["m"],))


# =====================================================================================
# 9. Contracts
# =====================================================================================
from pydantic import ValidationError  # noqa: E402
from backend.schemas import contracts as K  # noqa: E402

MV = uuid.uuid4()
BD = dict(ensemble_disagreement=0.5, similarity_to_training=0.25, assay_noise=0.25)


def P(**o):
    d = dict(endpoint="sol", kind="regression", model_version_id=MV, ensemble_n=5, value=1.0, interval_low=0.5, interval_high=1.5,
             coverage=0.9, sample_size=10, method_note="m", breakdown=BD)
    d.update(o)
    return K.Prediction(**d)


def test_contract_verdict_and_unknown_fields_forbidden():
    for extra in ("verdict", "label", "traffic_light", "pass_fail", "recommendation", "score"):
        with pytest.raises(ValidationError):
            P(**{extra: "GOOD"})
    with pytest.raises(ValidationError):
        K.UncertaintyBreakdown(**BD, verdict="x")


@pytest.mark.parametrize("n", [0, 1, 2, 4, 6, 7, 11, 21, 100, -5, "5", "five", 5.5, None, True, [5]])
def test_ensemble_n_bypass_rejected(n):
    with pytest.raises(ValidationError):
        P(ensemble_n=n)
    with pytest.raises(ValidationError):
        K.CompoundSubmission(smiles=["C"], endpoints=["sol"], ensemble_n=n)


@pytest.mark.parametrize("n", [3, 5, 10, 20])
def test_ensemble_n_valid(n):
    assert P(ensemble_n=n).ensemble_n == n


def test_float_ensemble_n_5_0_is_coerced_ok_or_rejected_but_never_other():
    try:
        assert P(ensemble_n=5.0).ensemble_n == 5
    except ValidationError:
        pass


@pytest.mark.parametrize("kw", [dict(value=float("nan")), dict(value=float("inf")), dict(interval_low=float("-inf"), interval_high=float("inf")),
                                dict(interval_low=float("nan"), interval_high=float("nan")), dict(interval_low=1.0, interval_high=float("nan"))])
def test_prediction_rejects_non_finite_floats(kw):
    with pytest.raises(ValidationError):
        P(**kw)


def test_nan_does_not_serialise_silently():
    try:
        out = P(value=float("nan")).model_dump_json()
    except Exception:
        return
    assert json.loads(out)["value"] is not None


@pytest.mark.parametrize("kw", [dict(coverage=float("nan")), dict(coverage=float("inf")), dict(coverage=1.0), dict(coverage=0.0),
                                dict(kind="classification", probability=float("nan")), dict(sample_size=0), dict(method_note=""),
                                dict(kind="regression", interval_low=2.0, interval_high=1.0), dict(kind="regression", interval_low=None)])
def test_prediction_core_contract_holds(kw):
    with pytest.raises(ValidationError):
        P(**kw)


@pytest.mark.parametrize("kw", [dict(value=99.0), dict(method_note="   "), dict(endpoint=""), dict(endpoint="x" * 10000),
                                dict(kind="classification", probability=0.5, interval_low=1.0, interval_high=2.0)])
def test_prediction_semantic_gaps(kw):
    with pytest.raises(ValidationError):
        P(**kw)


@pytest.mark.parametrize("bd", [dict(ensemble_disagreement=float("nan"), similarity_to_training=0.5, assay_noise=0.5),
                                dict(ensemble_disagreement=float("inf"), similarity_to_training=0.0, assay_noise=0.0),
                                dict(ensemble_disagreement=0.5, similarity_to_training=0.5, assay_noise=0.5),
                                dict(ensemble_disagreement=-0.1, similarity_to_training=0.6, assay_noise=0.5)])
def test_breakdown_rejects_bad_shares(bd):
    with pytest.raises(ValidationError):
        K.UncertaintyBreakdown(**bd)


def test_breakdown_tolerance_is_display_rounding_wide_but_bounded():
    with pytest.raises(ValidationError):
        K.UncertaintyBreakdown(ensemble_disagreement=0.5 + 5e-3, similarity_to_training=0.25, assay_noise=0.25)


def test_breakdown_tolerates_display_rounding():
    K.UncertaintyBreakdown(ensemble_disagreement=0.3333, similarity_to_training=0.3333, assay_noise=0.3333)


def S(**o):
    d = dict(smiles=["CCO"], endpoints=["sol"])
    d.update(o)
    return K.CompoundSubmission(**d)


def test_submission_count_and_blank_rules():
    with pytest.raises(ValidationError):
        S(smiles=["C"] * 11)
    with pytest.raises(ValidationError):
        S(smiles=[])
    for bad in ("", "   ", "C" * 2001):
        with pytest.raises(ValidationError):
            S(smiles=[bad])
    with pytest.raises(ValidationError):
        S(endpoints=[])
    with pytest.raises(ValidationError):
        S(smiles="CCO")


@pytest.mark.parametrize("kw", [dict(smiles=["C\x00C"]), dict(smiles=["C\nC"]), dict(smiles=["C\x1b[31mC"]), dict(smiles=["C‮C"]),
                                dict(smiles=["C​C"]), dict(smiles=["=cmd|' /C calc'!A0"]), dict(smiles=["C\tC"]),
                                dict(endpoints=[""]), dict(endpoints=["sol"] * 1000), dict(endpoints=["x" * 100000])])
def test_submission_hygiene(kw):
    with pytest.raises(ValidationError):
        S(**kw)


def test_submission_duplicates():
    with pytest.raises(ValidationError):
        S(smiles=["CCO"] * 10)


@bug("F-30 LOW", "tier N limits are not part of the model: CompoundSubmission(ensemble_n=20) validates for a free user; API must call allowed_ensemble_sizes")
def test_submission_cannot_exceed_free_tier_without_context():
    with pytest.raises(ValidationError):
        S(ensemble_n=20)  # no tier context exists to check; free is capped at 10


def test_allowed_sizes_by_tier():
    assert K.allowed_ensemble_sizes("free") == (3, 5, 10)
    assert K.allowed_ensemble_sizes("edu") == (3, 5, 10, 20)
    with pytest.raises(KeyError):
        K.allowed_ensemble_sizes("admin")


@pytest.mark.parametrize("make", [
    lambda: K.RankingConfig(weights=[K.PropertyWeight(endpoint="a", weight=float("inf"), higher_is_better=True)]),
    lambda: K.RankingConfig(weights=[K.PropertyWeight(endpoint="a", weight=1, higher_is_better=True)], limits=[K.HardLimit(endpoint="a", min=float("nan"))]),
    lambda: K.RankingConfig(weights=[K.PropertyWeight(endpoint="a", weight=1, higher_is_better=True)], limits=[K.HardLimit(endpoint="a", min=5, max=1)]),
    lambda: K.RankingConfig(weights=[K.PropertyWeight(endpoint="a", weight=1, higher_is_better=True)] * 2),
])
def test_ranking_config_gaps(make):
    with pytest.raises(ValidationError):
        make()


def test_ranking_config_core_rules():
    for kw in (dict(weights=[]), dict(lam=-0.1), dict(lam=1.1), dict(lam=float("nan"))):
        base = dict(weights=[K.PropertyWeight(endpoint="a", weight=1, higher_is_better=True)])
        base.update(kw)
        with pytest.raises(ValidationError):
            K.RankingConfig(**base)
    with pytest.raises(ValidationError):
        K.RankingConfig(weights=[K.PropertyWeight(endpoint="a", weight=0, higher_is_better=True)])
    with pytest.raises(ValidationError):
        K.PropertyWeight(endpoint="a", weight=-1, higher_is_better=True)
    with pytest.raises(ValidationError):
        K.PropertyWeight(endpoint="a", weight=float("nan"), higher_is_better=True)
