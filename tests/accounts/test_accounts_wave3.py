"""Wave 3 account endpoints: change password, terms status/accept, GET /account, login terms flag."""

import pytest

from backend.accounts.config import COOKIE_SESSION, AccountsConfig

PW = "correct horse battery"
TERMS = "2026-01"
NEW_PW = "another long passphrase 42"


def _new(app):
    """A second, cookie-less client over the same app (the conftest client class, found via the fixture)."""
    return _CLIENT_CLS[0](app)


_CLIENT_CLS: list = []


@pytest.fixture(autouse=True)
def _client_cls(client):
    _CLIENT_CLS[:] = [type(client)]


def _bump_terms(app, version="2026-02"):
    app.state.accounts_config = AccountsConfig(bcrypt_cost=4, terms_version=version, cookie_secure=True)


# ------------------------------------------------------------------ change password
def test_change_password_happy_path_keeps_current_session_and_kills_others(client, client2, app):
    client.signup_login()
    assert client2.login().status_code == 200  # second device, same account
    old_cookie = client.http.cookies.get(COOKIE_SESSION)
    r = client.post("/auth/change-password", {"current_password": PW, "new_password": NEW_PW})
    assert r.status_code == 200 and r.json()["status"] == "password_changed"
    assert client.http.cookies.get(COOKIE_SESSION) != old_cookie  # re-issued
    assert client.get("/_whoami").status_code == 200  # current session continues
    assert client.post("/_mutate").status_code == 200  # ...with a working CSRF token bound to the new session
    assert client2.get("/_whoami").status_code == 401  # other session invalidated via the pw-version claim
    # the OLD cookie value is dead too (explicit revocation of the replaced jti)
    stale = _new(app)
    stale.http.cookies.set(COOKIE_SESSION, old_cookie)
    assert stale.get("/_whoami").status_code == 401
    fresh = _new(app)
    assert fresh.login(pw=PW).status_code == 401
    assert fresh.login(pw=NEW_PW).status_code == 200


def test_change_password_requires_current_password(client, su):
    client.signup_login()
    r = client.post("/auth/change-password", {"current_password": "wrong password!!", "new_password": NEW_PW})
    assert r.status_code == 403 and r.json()["detail"] == "password_incorrect"
    assert su.execute("SELECT count(*) FROM audit_log WHERE action = 'password_change'").fetchone()[0] == 0
    assert client.login(pw=PW).status_code == 200


@pytest.mark.parametrize(
    "bad", ["short", "password1234", "a@example.com", "aaaaaaaaaaaa", PW]
)
def test_change_password_applies_the_password_policy(client, bad):
    client.signup_login()
    r = client.post("/auth/change-password", {"current_password": PW, "new_password": bad})
    assert r.status_code == 422
    assert client.login(pw=PW).status_code == 200  # unchanged


def test_change_password_needs_auth_and_csrf(client, app):
    anon = _new(app)
    assert anon.post("/auth/change-password", {"current_password": PW, "new_password": NEW_PW}).status_code == 401
    client.signup_login()
    r = client.post("/auth/change-password", {"current_password": PW, "new_password": NEW_PW}, csrf=False)
    assert r.status_code == 403 and r.json()["detail"] == "csrf_failed"
    r = client.http.post(
        "/auth/change-password", json={"current_password": PW, "new_password": NEW_PW, "x": 1}, headers=client.h()
    )
    assert r.status_code == 422  # extra fields refused


def test_change_password_is_audited_and_rate_limited(client, su, app):
    app.state.accounts_config = AccountsConfig(
        bcrypt_cost=4, terms_version=TERMS, cookie_secure=True, rl_change_password_user=(2, 900)
    )
    client.signup_login()
    codes = [
        client.post("/auth/change-password", {"current_password": "nope nope nope", "new_password": NEW_PW}).status_code
        for _ in range(4)
    ]
    assert codes == [403, 403, 429, 429]
    ok = su.execute("SELECT count(*) FROM audit_log WHERE action = 'password_change'").fetchone()[0]
    assert ok == 0


def test_change_password_audit_row(client, su):
    client.signup_login()
    assert client.post("/auth/change-password", {"current_password": PW, "new_password": NEW_PW}).status_code == 200
    assert su.execute("SELECT count(*) FROM audit_log WHERE action = 'password_change'").fetchone()[0] == 1


def test_change_password_allowed_with_stale_terms(client, app):
    client.signup_login()
    _bump_terms(app)
    assert client.post("/auth/change-password", {"current_password": PW, "new_password": NEW_PW}).status_code == 200


# ------------------------------------------------------------------ terms
def test_terms_status_and_accept(client, app):
    client.signup_login()
    st = client.get("/auth/terms/status").json()
    assert st == {
        "required_version": TERMS,
        "accepted_versions": {"terms": TERMS, "privacy": TERMS, "disclaimer": TERMS},
        "reaccept_required": False,
    }
    _bump_terms(app)
    st = client.get("/auth/terms/status").json()
    assert st["reaccept_required"] is True and st["required_version"] == "2026-02"
    assert client.post("/auth/terms/accept", {"version": TERMS}).status_code == 400
    assert client.post("/auth/terms/accept", {"version": "2026-02"}).status_code == 200
    st = client.get("/auth/terms/status").json()
    assert st["reaccept_required"] is False and set(st["accepted_versions"].values()) == {"2026-02"}


def test_terms_endpoints_require_auth_and_csrf(client, app):
    anon = _new(app)
    assert anon.get("/auth/terms/status").status_code == 401
    assert anon.post("/auth/terms/accept", {"version": TERMS}).status_code == 401
    client.signup_login()
    assert client.post("/auth/terms/accept", {"version": TERMS}, csrf=False).status_code == 403


def test_login_reports_terms_reaccept_and_domain_routes_refuse_until_accepted(client, app):
    client.signup_login()
    _bump_terms(app)
    other = _new(app)
    r = other.login()
    assert r.status_code == 200 and r.json()["terms_reaccept_required"] is True
    assert other.get("/auth/me").json()["terms_reaccept_required"] is True
    # mutating domain-style routes (guarded by current_user) refuse with a stable code
    r = other.post("/_mutate")
    assert r.status_code == 403 and r.json()["detail"] == "terms_reacceptance_required"
    assert other.post("/auth/terms/accept", {"version": "2026-02"}).status_code == 200
    assert other.post("/_mutate").status_code == 200
    again = _new(app)
    assert again.login().json()["terms_reaccept_required"] is False


def test_login_flag_false_when_current(client):
    client.signup_login()
    r = _new(client.app).login()
    assert r.json()["terms_reaccept_required"] is False


# ------------------------------------------------------------------ GET /account
def test_account_summary_free_user(client):
    uid = client.signup_login("a@example.com")
    client.post("/consents", {"scope": "analytics", "granted": True})
    body = client.get("/account").json()
    assert body["id"] == str(uid) and body["tier"] == "free"
    assert body["email_verified"] is True and body["is_edu"] is False
    assert body["caps"]["max_ensemble_n"] == 10 and body["caps"]["allowed_ensemble_sizes"] == [3, 5, 10]
    assert body["caps"]["max_compounds_per_project"] == 10
    assert body["consents"] == {"train_models": False, "share_dataset": False, "analytics": True, "contact": False}
    assert body["terms"]["reaccept_required"] is False
    assert "password" not in str(body).lower() and "a@example.com" not in str(body)


def test_account_summary_edu_user(client):
    client.signup_login("student@uni.edu")
    body = client.get("/account").json()
    assert body["tier"] == "edu" and body["is_edu"] is True and body["caps"]["max_ensemble_n"] == 20


def test_account_requires_auth_and_works_with_stale_terms(client, app):
    assert _new(app).get("/account").status_code == 401
    client.signup_login()
    _bump_terms(app)
    r = client.get("/account")
    assert r.status_code == 200 and r.json()["terms"]["reaccept_required"] is True


def test_account_is_per_user(client, client2):
    client.signup_login("a@example.com")
    client2.signup_login("b@uni.edu")
    assert client.get("/account").json()["tier"] == "free"
    assert client2.get("/account").json()["tier"] == "edu"
