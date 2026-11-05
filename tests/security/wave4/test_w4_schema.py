"""Wave 4: migration 0005 (Lead Studio tables) from the privilege side. test_lead_rls.py shows another user's identity sees nothing; this adds the
catalogue facts that make that robust (forced RLS, exactly one owner policy, no extra grants for any role, server-set columns), the CHECK
constraints (enforced even for the owner), and the failure modes of a missing or malformed identity."""
from __future__ import annotations

import json

import psycopg
import pytest
from test_schema_redteam import reg, rejected

TABLES = ("lead_saved_run", "lead_manual_session", "lead_manual_step", "lead_profile")
EV = json.dumps([{"type": "run_start", "run_id": "r", "seq": 0, "payload": {}}])
PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def test_row_level_security_is_enabled_and_forced_with_one_owner_policy_each(su):
    for t in TABLES:
        en, force, owner = su.execute("SELECT relrowsecurity, relforcerowsecurity, relowner::regrole::text FROM pg_class WHERE relname=%s", (t,)).fetchone()
        assert en and force, t
        assert owner not in ("metis_app", "metis_readonly", "metis_auth", "metis_janitor"), (t, owner)
        pol = su.execute("SELECT polname, polcmd, polroles::text, pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid) FROM pg_policy "
                         "WHERE polrelid = %s::regclass", (t,)).fetchall()
        assert len(pol) == 1, (t, pol)
        name, cmd, roles, using, check = pol[0]
        assert name == f"{t}_owner" and cmd == "*" and roles == "{0}"           # one policy, all commands, all roles
        assert "owner_id = current_user_id()" in using and "owner_id = current_user_id()" in check


def test_no_role_but_metis_app_holds_any_privilege_and_metis_app_holds_only_what_the_api_needs(su):
    cols = {t: [r[0] for r in su.execute("SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped", (t,))] for t in TABLES}
    for t in TABLES:
        for role in ("metis_readonly", "metis_auth", "metis_janitor", "public"):
            for pr in PRIVS:
                assert not su.execute("SELECT has_table_privilege(%s, %s, %s)", (role, t, pr)).fetchone()[0], (role, t, pr)
            for col in cols[t]:
                for pr in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                    assert not su.execute("SELECT has_column_privilege(%s, %s, %s, %s)", (role, t, col, pr)).fetchone()[0], (role, t, col, pr)
        for pr in ("TRUNCATE", "REFERENCES", "TRIGGER"):
            assert not su.execute("SELECT has_table_privilege('metis_app', %s, %s)", (t, pr)).fetchone()[0], (t, pr)
        assert su.execute("SELECT has_table_privilege('metis_app', %s, 'SELECT')", (t,)).fetchone()[0]
        assert su.execute("SELECT has_table_privilege('metis_app', %s, 'DELETE')", (t,)).fetchone()[0]
        assert not su.execute("SELECT has_table_privilege('metis_app', %s, 'INSERT')", (t,)).fetchone()[0]      # column-level only
        assert not su.execute("SELECT has_table_privilege('metis_app', %s, 'UPDATE')", (t,)).fetchone()[0]
    ins = {t: {c for c in cols[t] if su.execute("SELECT has_column_privilege('metis_app', %s, %s, 'INSERT')", (t, c)).fetchone()[0]} for t in TABLES}
    upd = {t: {c for c in cols[t] if su.execute("SELECT has_column_privilege('metis_app', %s, %s, 'UPDATE')", (t, c)).fetchone()[0]} for t in TABLES}
    assert upd == {"lead_saved_run": set(), "lead_manual_session": set(), "lead_manual_step": set(), "lead_profile": {"name", "body", "updated_at"}}
    for t in TABLES:
        assert not ins[t] & {"id", "created_at", "updated_at"}, (t, ins[t])          # ids and timestamps are always server-made
    assert ins["lead_saved_run"] == {"owner_id", "title", "stage", "target_id", "seed_smiles", "headline", "events"}
    assert ins["lead_profile"] == {"owner_id", "name", "body"}


def test_the_app_role_cannot_choose_ids_or_timestamps_or_weaken_the_tables(appc, su):
    u = reg(su, "u@x.com")
    c = appc(u)
    assert rejected(c, "INSERT INTO lead_profile(id, owner_id, name, body) VALUES (gen_random_uuid(), %s, 'p', '{}')", (u,))
    assert rejected(c, "INSERT INTO lead_profile(owner_id, name, body, created_at) VALUES (%s, 'p', '{}', now() - interval '3 years')", (u,))
    assert c.execute("INSERT INTO lead_profile(owner_id, name, body) VALUES (%s, 'p', '{}') RETURNING 1", (u,)).fetchone()
    assert rejected(c, "UPDATE lead_profile SET created_at = now() - interval '1 day'")
    assert rejected(c, "UPDATE lead_profile SET owner_id = gen_random_uuid()")
    assert rejected(c, "UPDATE lead_profile SET id = gen_random_uuid()")
    assert c.execute("UPDATE lead_profile SET name='q', body='{\"a\":1}', updated_at=now()").rowcount == 1
    for t in TABLES:
        assert rejected(c, f"TRUNCATE {t}")  # noqa: S608
        assert rejected(c, f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")  # noqa: S608
        assert rejected(c, f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")  # noqa: S608
        assert rejected(c, f"DROP POLICY {t}_owner ON {t}")  # noqa: S608
        assert rejected(c, f"CREATE POLICY open ON {t} USING (true)")  # noqa: S608
        rejected(c, f"GRANT SELECT ON {t} TO PUBLIC")  # noqa: S608 - only a WARNING without grant option; the catalogue check below is what counts
        assert rejected(c, f"DROP TABLE {t}")  # noqa: S608
    for t in TABLES:
        assert not su.execute("SELECT has_table_privilege('public', %s, 'SELECT')", (t,)).fetchone()[0]
        assert not su.execute("SELECT has_table_privilege('metis_readonly', %s, 'SELECT')", (t,)).fetchone()[0]
    assert rejected(c, "UPDATE lead_saved_run SET title='x'") and rejected(c, "UPDATE lead_manual_step SET seq=1")
    assert rejected(c, "CREATE TRIGGER x BEFORE INSERT ON lead_profile FOR EACH ROW EXECUTE FUNCTION current_user_id()")


def test_a_missing_or_garbled_identity_returns_no_rows_and_accepts_no_rows(appc, su):
    u = reg(su, "u@x.com")
    su.execute("INSERT INTO lead_profile(owner_id,name,body) VALUES (%s,'secret','{}')", (u,))
    for ident in ("", "not-a-uuid", "00000000-0000-0000-0000-000000000000", "' OR '1'='1", u.__str__().upper() + " ", "null"):
        c = appc(None)
        try:
            c.execute("SELECT set_config('metis.user_id', %s, false)", (ident,))
            rows = c.execute("SELECT count(*) FROM lead_profile").fetchone()[0]
        except psycopg.Error:
            rows = 0                                     # an error is as good as nothing: no data is returned either way
        assert rows == 0 or ident.strip().lower() == str(u), repr(ident)
        assert rejected(c, "INSERT INTO lead_profile(owner_id,name,body) VALUES (%s,'planted','{}')", (u,)), repr(ident)


CHECKS = [
    ("lead_saved_run", "INSERT INTO lead_saved_run(owner_id,title,stage,events) VALUES (%s,{t},'pharmacology',%s::jsonb)", [
        ("''", EV), ("'   '", EV), ("'a'||chr(10)||'b'", EV), ("'a'||chr(0)", EV), ("repeat('x',121)", EV), ("'ok'", "{}"), ("'ok'", "[]"), ("'ok'", "5"),
        ("'ok'", "null"), ("'ok'", json.dumps([0] * 5001)), ("'ok'", json.dumps(["x" * 1_950_000]))]),
    ("lead_profile", "INSERT INTO lead_profile(owner_id,name,body) VALUES (%s,{t},%s::jsonb)", [
        ("''", "{}"), ("repeat('x',81)", "{}"), ("'a'||chr(9)", "{}"), ("'ok'", "[]"), ("'ok'", "5"), ("'ok'", json.dumps({"k": "x" * 17000}))]),
]


@pytest.mark.parametrize("table,sql,cases", CHECKS, ids=[c[0] for c in CHECKS])
def test_check_constraints_hold_even_for_the_schema_owner(su, table, sql, cases):
    u = reg(su, "u@x.com")
    for title, payload in cases:
        # `title` is a SQL expression; keep it inside the statement, bind the rest
        assert rejected(su, sql.format(t=title), (u, payload)), (table, title, payload[:40])
    assert su.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


def test_other_columns_reject_what_the_api_rejects(su):
    u = reg(su, "u@x.com")
    s = su.execute("INSERT INTO lead_manual_session(owner_id,seed_smiles) VALUES (%s,'CCO') RETURNING id", (u,)).fetchone()[0]
    step = ("INSERT INTO lead_manual_step(owner_id,session_id,seq,parent_smiles,child_smiles,edit_id,site,reward_delta,t) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)")
    ok = (u, s, 0, "CCO", "CCN", "e", [1], 0.5, 1.0)
    assert not rejected(su, step, ok)
    for i, bad in ((2, -1), (2, 500), (3, ""), (3, "C" * 2001), (4, ""), (5, "a b"), (5, "x" * 81), (5, ""), (6, list(range(65))), (7, float("nan")),
                   (7, float("inf")), (7, 1e7), (7, -1e7), (8, -1.0), (8, 1e10), (8, float("nan"))):
        row = list(ok)
        row[1], row[2] = s, 1 if i != 2 else row[2]
        row[i] = bad
        assert rejected(su, step, tuple(row)), (i, bad)
    assert rejected(su, step, (u, s, 0, "CCO", "CCN", "dup", [1], 0.5, 1.0))                  # (session_id, seq) is unique
    assert rejected(su, "INSERT INTO lead_manual_session(owner_id,seed_smiles,target_id) VALUES (%s,'CCO','a b')", (u,))
    assert rejected(su, "INSERT INTO lead_manual_session(owner_id,seed_smiles) VALUES (%s,'')", (u,))
    assert rejected(su, "INSERT INTO lead_saved_run(owner_id,title,stage,events,target_id) VALUES (%s,'t','pharmacology',%s::jsonb,'../x')", (u, EV))
    assert rejected(su, "INSERT INTO lead_saved_run(owner_id,title,stage,events) VALUES (%s,'t','manual',%s::jsonb)", (u, EV))
    assert rejected(su, "INSERT INTO lead_saved_run(owner_id,title,stage,events) VALUES (NULL,'t','binding',%s::jsonb)", (EV,))
    assert rejected(su, "INSERT INTO lead_saved_run(owner_id,title,stage,events) VALUES (gen_random_uuid(),'t','binding',%s::jsonb)", (EV,))   # FK to app_user


def test_deleting_a_session_or_an_account_removes_every_dependent_row_even_with_forced_rls(su, appc):
    u, other = reg(su, "u@x.com"), reg(su, "o@x.com")
    s = su.execute("INSERT INTO lead_manual_session(owner_id,seed_smiles) VALUES (%s,'CCO') RETURNING id", (u,)).fetchone()[0]
    for i in range(3):
        su.execute("INSERT INTO lead_manual_step(owner_id,session_id,seq,parent_smiles,child_smiles,edit_id,site) VALUES (%s,%s,%s,'CCO','CCN','e',%s)", (u, s, i, [0]))
    keep = su.execute("INSERT INTO lead_manual_session(owner_id,seed_smiles) VALUES (%s,'CCC') RETURNING id", (other,)).fetchone()[0]
    su.execute("INSERT INTO lead_manual_step(owner_id,session_id,seq,parent_smiles,child_smiles,edit_id,site) VALUES (%s,%s,0,'CCC','CCN','e',%s)", (other, keep, [0]))
    c = appc(u)
    assert c.execute("DELETE FROM lead_manual_session WHERE id=%s", (s,)).rowcount == 1
    assert su.execute("SELECT count(*) FROM lead_manual_step WHERE session_id=%s", (s,)).fetchone()[0] == 0
    assert su.execute("SELECT count(*) FROM lead_manual_step WHERE owner_id=%s", (other,)).fetchone()[0] == 1
    # the unverified-account janitor deletes users through the same cascade: it must not be blocked by the forced policies
    su.execute("INSERT INTO lead_profile(owner_id,name,body) VALUES (%s,'p','{}')", (u,))
    su.execute("DELETE FROM app_user WHERE id=%s", (u,))
    assert su.execute("SELECT count(*) FROM lead_profile WHERE owner_id=%s", (u,)).fetchone()[0] == 0
