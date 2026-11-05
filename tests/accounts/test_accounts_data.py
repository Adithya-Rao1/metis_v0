"""Consents, terms re-acceptance, export, deletion, isolation, log redaction."""

import io
import json
import logging

import pytest

from backend.accounts.config import AccountsConfig
from backend.core.logging import configure_logging

PW = "correct horse battery"
PW2 = "another long passphrase"
TERMS = "2026-01"


def seed(su, uid, tag):
    """Insert tenant rows for a user using the superuser connection."""
    pid = su.execute(
        "INSERT INTO project (owner_id, name) VALUES (%s, %s) RETURNING id", (uid, f"proj-{tag}")
    ).fetchone()[0]
    su.execute(
        "INSERT INTO compound (owner_id, project_id, smiles, canonical_smiles) VALUES (%s,%s,%s,%s)",
        (uid, pid, "CCO", "CCO"),
    )
    return pid


# ------------------------------------------------------------ consents
def test_consents_default_none(client):
    client.signup_login()
    r = client.get("/consents").json()
    assert set(r["consents"]) == {"train_models", "share_dataset", "analytics", "contact"}
    assert all(not v["granted"] for v in r["consents"].values()) and r["history"] == []


def test_consent_grant_revoke_one_active_append_only(client, su):
    client.signup_login()
    assert client.post("/consents", {"scope": "analytics", "granted": True}).json()["consents"]["analytics"][
        "granted"
    ]
    client.post("/consents", {"scope": "analytics", "granted": True})  # idempotent, still one active
    assert su.execute("SELECT count(*) FROM consent WHERE revoked_at IS NULL").fetchone()[0] == 1
    r = client.post("/consents", {"scope": "analytics", "granted": False}).json()
    assert not r["consents"]["analytics"]["granted"]
    r = client.post("/consents", {"scope": "analytics", "granted": True}).json()
    assert r["consents"]["analytics"]["granted"] and len(r["history"]) == 2  # re-grant appends a row
    assert su.execute("SELECT count(*) FROM consent").fetchone()[0] == 2
    assert su.execute("SELECT count(*) FROM audit_log WHERE action LIKE 'consent_%%'").fetchone()[0] == 4


def test_consent_bad_scope_and_unauthenticated(client):
    assert client.post("/consents", {"scope": "everything", "granted": True}).status_code in (401, 422)
    assert client.get("/consents").status_code == 401
    client.signup_login()
    assert client.post("/consents", {"scope": "everything", "granted": True}).status_code == 422


def test_consent_isolation(client, client2):
    client.signup_login("a@example.com")
    client2.signup_login("b@example.com")
    client.post("/consents", {"scope": "train_models", "granted": True})
    assert client2.get("/consents").json()["consents"]["train_models"]["granted"] is False
    client2.post("/consents", {"scope": "train_models", "granted": False})  # revoking own does not touch A
    assert client.get("/consents").json()["consents"]["train_models"]["granted"] is True


# ----------------------------------------------------------- re-accept
def test_terms_reacceptance_flow(client, app):
    client.signup_login()
    assert client.post("/_mutate").status_code == 200
    app.state.accounts_config = AccountsConfig(bcrypt_cost=4, terms_version="2026-02", cookie_secure=True)
    r = client.post("/_mutate")
    assert r.status_code == 403 and r.json()["detail"] == "terms_reacceptance_required"
    assert client.post("/consents", {"scope": "analytics", "granted": True}).status_code == 403
    # Wave 4 (F-123): reads are refused too until the terms are re-accepted; the account routes stay reachable.
    assert client.get("/_whoami").status_code == 403
    assert client.get("/_whoami").json()["detail"] == "terms_reacceptance_required"
    assert client.get("/consents").status_code == 403
    assert client.get("/auth/me").json()["terms_current"] is False
    assert client.post("/terms/accept", {"version": TERMS}).status_code == 400  # stale version
    assert client.post("/terms/accept", {"version": "2026-02"}).status_code == 200
    assert client.get("/auth/me").json()["terms_current"] is True
    assert client.post("/_mutate").status_code == 200


def test_reacceptance_stores_new_rows_keeping_history(client, app, su):
    client.signup_login()
    app.state.accounts_config = AccountsConfig(bcrypt_cost=4, terms_version="2026-02", cookie_secure=True)
    client.post("/terms/accept", {"version": "2026-02"})
    rows = su.execute(
        "SELECT document, version FROM terms_acceptance ORDER BY accepted_at, document"
    ).fetchall()
    assert len(rows) == 6 and {v for _, v in rows} == {TERMS, "2026-02"}


def test_logout_and_delete_allowed_with_stale_terms(client, app):
    client.signup_login()
    app.state.accounts_config = AccountsConfig(bcrypt_cost=4, terms_version="2026-02", cookie_secure=True)
    assert client.delete("/account/data", {"password": PW}).status_code == 200


def test_terms_version_endpoint(client):
    assert client.get("/auth/terms").json()["version"] == TERMS


# ------------------------------------------------------------- export
def test_export_contains_only_own_data(client, client2, su):
    a = client.signup_login("a@example.com")
    b = client2.signup_login("b@example.com")
    pa, pb = seed(su, a, "A"), seed(su, b, "B")
    su.execute(
        "INSERT INTO grant_ (owner_id, project_id, grantee_id, permission) VALUES (%s,%s,%s,'view')",
        (b, pb, a),
    )
    client.post("/consents", {"scope": "contact", "granted": True})
    r = client.get("/account/data")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    data = r.json()
    text = r.text
    assert data["account"]["id"] == str(a) and "password_hash" not in text
    assert [p["id"] for p in data["project"]] == [str(pa)]  # shared-in project of B is not A's data
    assert len(data["compound"]) == 1 and data["compound"][0]["owner_id"] == str(a)
    assert str(b) not in text and "b@example.com" not in text and str(pb) not in text
    assert len(data["terms_acceptance"]) == 3 and len(data["consents"]) == 1
    assert {x["action"] for x in data["audit_log"]} >= {"register", "login_ok"}
    # shared-in grants are not exported as A's grants
    assert data["grants"] == []
    assert json.loads(client2.get("/account/data").text)["grants"][0]["grantee_id"] == str(a)


def test_export_requires_auth(client):
    assert client.get("/account/data").status_code == 401


# ------------------------------------------------------------- delete
def test_delete_requires_password(client, su):
    uid = client.signup_login()
    assert client.delete("/account/data", {"password": "wrong password!"}).status_code == 403
    assert su.execute("SELECT count(*) FROM app_user WHERE id=%s", (uid,)).fetchone()[0] == 1
    assert su.execute("SELECT count(*) FROM deletion_request").fetchone()[0] == 0


def test_delete_removes_everything_and_records_request(client, su):
    uid = client.signup_login()
    seed(su, uid, "A")
    client.post("/consents", {"scope": "analytics", "granted": True})
    client.post("/auth/logout")  # creates a session_revocation row too
    client.login()
    r = client.delete("/account/data", {"password": PW})
    assert r.status_code == 200 and r.json()["status"] == "deleted"
    for t in (
        "app_user",
        "auth_token",
        "terms_acceptance",
        "consent",
        "project",
        "compound",
        "session_revocation",
    ):
        assert su.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0, t  # noqa: S608
    dr = su.execute(
        "SELECT owner_id, scope, backups_purge_due - requested_at FROM deletion_request"
    ).fetchall()
    assert len(dr) == 1 and dr[0][0] == uid and dr[0][1] == "account" and dr[0][2].days == 30
    assert (
        su.execute(
            "SELECT count(*) FROM audit_log WHERE action='account_delete' AND actor_id=%s", (uid,)
        ).fetchone()[0]
        == 1
    )
    assert client.get("/_whoami").status_code == 401  # old session dead
    assert client.login().status_code == 401  # cannot log in again


def test_delete_leaves_other_users_intact(client, client2, su):
    a = client.signup_login("a@example.com")
    b = client2.signup_login("b@example.com")
    seed(su, a, "A")
    seed(su, b, "B")
    client.delete("/account/data", {"password": PW})
    assert su.execute("SELECT count(*) FROM project WHERE owner_id=%s", (b,)).fetchone()[0] == 1
    assert client2.get("/_whoami").status_code == 200


def test_delete_with_other_users_password_rejected(client, client2):
    client.signup_login("a@example.com", PW)
    client2.signup_login("b@example.com", PW2)
    assert client.delete("/account/data", {"password": PW2}).status_code == 403


def test_delete_password_attempts_rate_limited(client, cfg):
    cfg.rl_delete_user = (2, 900)
    client.signup_login()
    codes = [client.delete("/account/data", {"password": "bad password 123"}).status_code for _ in range(4)]
    assert codes == [403, 403, 429, 429]


# ---------------------------------------------------- cross-user isolation
def test_sessions_are_user_scoped(client, client2):
    a = client.signup_login("a@example.com")
    b = client2.signup_login("b@example.com")
    assert client.get("/_whoami").json()["id"] == str(a)
    assert client2.get("/_whoami").json()["id"] == str(b)
    client.post("/auth/logout")
    assert client2.get("/_whoami").status_code == 200  # A's revocation does not affect B


def test_logout_other_users_jti_cannot_be_revoked_via_rls(app, factory, client, client2):
    """A user's session_revocation insert is bound to their own id by RLS."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from backend.core.db import request_session

    a = client.signup_login("a@example.com")
    b = client2.signup_login("b@example.com")
    with pytest.raises(DBAPIError), request_session(factory, a) as s:
        s.execute(
            text(
                "INSERT INTO session_revocation (jti, owner_id, expires_at) VALUES (gen_random_uuid(), :b, now())"
            ),
            {"b": b},
        )


def test_identity_is_transaction_local(factory, client):
    """No identity leaks onto pooled connections between requests."""
    from sqlalchemy import text

    uid = client.signup_login()
    client.get("/_whoami")
    with factory() as s:
        assert s.execute(text("SELECT current_setting('metis.user_id', true)")).scalar() in (None, "")
    del uid


# ------------------------------------------------------- log redaction
def test_no_emails_tokens_or_passwords_in_logs(client):
    buf = io.StringIO()
    configure_logging("DEBUG", stream=buf)
    try:
        client.register("secret.person@example.com", PW)
        token = client.token_for("secret.person@example.com")
        client.verify("secret.person@example.com")
        client.login("secret.person@example.com", "wrong password here")
        client.login("secret.person@example.com", PW)
        client.post("/auth/logout")
        out = buf.getvalue()
        for needle in ("secret.person", PW, token, "wrong password", "metis_session"):
            assert needle not in out, needle
    finally:
        logging.getLogger().handlers = [
            h for h in logging.getLogger().handlers if not getattr(h, "_metis", False)
        ]


def test_console_mailer_logs_no_secrets(caplog):
    from backend.accounts.mailer import ConsoleMailer, InMemoryMailer, Mailer

    with caplog.at_level(logging.INFO):
        ConsoleMailer().send_verification("who@example.com", "tok-abc-123")
        ConsoleMailer().send_already_registered("who@example.com")
    assert "who@example.com" not in caplog.text and "tok-abc-123" not in caplog.text
    assert isinstance(ConsoleMailer(), Mailer) and isinstance(InMemoryMailer(), Mailer)
