"""Lead Studio tables (migration 0005): row-level security isolates users even if the application code were wrong.
Same method as test_schema_redteam.py: the attacker is the real least-privilege app role with ANOTHER user's identity."""
import json

import psycopg
import pytest
from test_schema_redteam import reg, rejected

EVENTS = json.dumps([{"type": "run_start", "run_id": "r", "seq": 0, "payload": {}}])


@pytest.fixture
def lw(su):
    """Victim V owns one of everything; attacker M and bystander N own nothing."""
    v, m, n = reg(su, "v@x.com"), reg(su, "m@x.com"), reg(su, "n@x.com")
    run = su.execute("INSERT INTO lead_saved_run(owner_id,title,stage,events) VALUES (%s,'secret run','pharmacology',%s::jsonb) RETURNING id",
                     (v, EVENTS)).fetchone()[0]
    ms = su.execute("INSERT INTO lead_manual_session(owner_id,seed_smiles) VALUES (%s,'CC(=O)Oc1ccccc1C(=O)O') RETURNING id", (v,)).fetchone()[0]
    su.execute("INSERT INTO lead_manual_step(owner_id,session_id,seq,parent_smiles,child_smiles,edit_id,site) VALUES (%s,%s,0,'CCO','CCN','e',%s)",
               (v, ms, [1]))
    prof = su.execute("INSERT INTO lead_profile(owner_id,name,body) VALUES (%s,'secret profile','{\"avoid_smarts\":[\"c1ccccc1\"]}') RETURNING id",
                      (v,)).fetchone()[0]
    return dict(v=v, m=m, n=n, run=run, ms=ms, prof=prof)


TABLES = ("lead_saved_run", "lead_manual_session", "lead_manual_step", "lead_profile")


def test_other_user_sees_nothing(appc, lw):
    c = appc(lw["m"])
    for t in TABLES:
        assert c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0, t  # noqa: S608 - fixed table names
        assert c.execute(f"SELECT count(*) FROM {t} WHERE owner_id = %s", (lw["v"],)).fetchone()[0] == 0, t  # noqa: S608
    assert c.execute("SELECT count(*) FROM lead_saved_run WHERE id=%s", (lw["run"],)).fetchone()[0] == 0
    assert appc(lw["v"]).execute("SELECT count(*) FROM lead_saved_run").fetchone()[0] == 1


def test_anonymous_and_unset_identity_see_nothing(appc, lw):
    for who in (None, ""):
        c = appc(who)
        for t in TABLES:
            assert c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0, t  # noqa: S608
            assert rejected(c, f"INSERT INTO {t}(owner_id) VALUES (%s)", (lw["v"],)), t  # noqa: S608


def test_cannot_modify_or_delete_foreign_rows(appc, lw):
    c = appc(lw["m"])
    assert c.execute("DELETE FROM lead_saved_run WHERE id=%s", (lw["run"],)).rowcount == 0
    assert c.execute("DELETE FROM lead_manual_session WHERE id=%s", (lw["ms"],)).rowcount == 0
    assert c.execute("DELETE FROM lead_manual_step").rowcount == 0
    assert c.execute("DELETE FROM lead_profile WHERE id=%s", (lw["prof"],)).rowcount == 0
    assert c.execute("UPDATE lead_profile SET name='pwned', body='{}'::jsonb WHERE id=%s", (lw["prof"],)).rowcount == 0
    v = appc(lw["v"])
    assert v.execute("SELECT name FROM lead_profile").fetchone()[0] == "secret profile"
    assert v.execute("SELECT count(*) FROM lead_manual_step").fetchone()[0] == 1


def test_cannot_plant_rows_in_another_users_name(appc, lw):
    c = appc(lw["m"])
    assert rejected(c, "INSERT INTO lead_saved_run(owner_id,title,stage,events) VALUES (%s,'x','pharmacology',%s::jsonb)", (lw["v"], EVENTS))
    assert rejected(c, "INSERT INTO lead_manual_session(owner_id,seed_smiles) VALUES (%s,'CCO')", (lw["v"],))
    assert rejected(c, "INSERT INTO lead_profile(owner_id,name,body) VALUES (%s,'x','{}')", (lw["v"],))


def test_cannot_hand_over_a_row_by_changing_owner(appc, lw):
    v = appc(lw["v"])
    assert rejected(v, "UPDATE lead_profile SET owner_id=%s WHERE id=%s", (lw["m"], lw["prof"]))


def test_step_cannot_attach_to_a_foreign_session(appc, lw):
    """Even with the victim's session id, the composite FK (session_id, owner_id) refuses a step owned by the attacker,
    and RLS refuses a step claiming the victim as owner."""
    c = appc(lw["m"])
    q = ("INSERT INTO lead_manual_step(owner_id,session_id,seq,parent_smiles,child_smiles,edit_id,site) "
         "VALUES (%s,%s,5,'CCO','CCN','e',%s)")
    assert rejected(c, q, (lw["m"], lw["ms"], [1]))
    assert rejected(c, q, (lw["v"], lw["ms"], [1]))


def test_readonly_role_cannot_read_lead_data(su, lw):
    for t in TABLES:
        c = psycopg.connect(su.info.dsn, autocommit=True)
        try:
            c.execute("SET ROLE metis_readonly")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f"SELECT * FROM {t}")  # noqa: S608
        finally:
            c.close()


def test_account_deletion_cascades_to_all_lead_data(su, appc, lw):
    assert appc(lw["v"]).execute("DELETE FROM app_user WHERE id=%s", (lw["v"],)).rowcount == 1
    for t in TABLES:
        assert su.execute(f"SELECT count(*) FROM {t} WHERE owner_id=%s", (lw["v"],)).fetchone()[0] == 0, t  # noqa: S608
