"""Register / verify / login / logout / JWT / CSRF / rate limits / lockout."""

import time
import uuid

import jwt
import pytest

from backend.accounts.config import COOKIE_SESSION, CSRF_HEADER, jwt_key

PW = "correct horse battery"
TERMS = "2026-01"


def count(su, sql, *a):
    return su.execute(sql, a).fetchone()[0]


# ------------------------------------------------------------ register
def test_register_without_terms_fails_and_creates_nothing(client, su):
    r = client.post("/auth/register", {"email": "a@example.com", "password": PW})
    assert r.status_code == 422
    r = client.register(version="")
    assert r.status_code == 422
    r = client.register(version="1999-01")  # not the current version
    assert r.status_code == 400 and r.json()["detail"] == "terms_acceptance_required"
    assert count(su, "SELECT count(*) FROM app_user") == 0
    assert count(su, "SELECT count(*) FROM terms_acceptance") == 0
    assert client.mailer.outbox == []


def test_register_stores_terms_version_for_all_documents(client, su):
    assert client.register().status_code == 202
    rows = su.execute("SELECT document, version FROM terms_acceptance ORDER BY document").fetchall()
    assert rows == [("disclaimer", TERMS), ("privacy", TERMS), ("terms", TERMS)]
    assert count(su, "SELECT count(*) FROM audit_log WHERE action='register'") == 1


def test_register_stores_only_token_hash_and_tier_free(client, su):
    client.register()
    token = client.token_for("a@example.com")
    stored = su.execute("SELECT token_hash FROM auth_token").fetchall()
    assert len(stored) == 1 and token not in stored[0][0] and len(stored[0][0]) == 64
    assert su.execute("SELECT tier, email_verified_at FROM app_user").fetchone() == ("free", None)


def test_register_response_uniform_for_existing_email(client, client2):
    a = client.register("dup@example.com")
    assert client.verify("dup@example.com").status_code == 200
    b = client2.register("DUP@example.com")  # case-insensitive duplicate of a verified account
    assert a.status_code == b.status_code == 202 and a.json() == b.json()
    assert [k for k, _, _ in client.mailer.outbox] == ["verify", "exists"]


def test_reregister_unverified_resends_verification_and_keeps_hash(client, client2, su):
    client.register("dup@example.com", pw="first correct horse")
    h1 = su.execute("SELECT password_hash FROM app_user").fetchone()[0]
    r = client2.register("dup@example.com", pw="attacker chosen pass")
    assert r.status_code == 202
    assert [k for k, _, _ in client.mailer.outbox] == ["verify", "verify"]
    assert su.execute("SELECT password_hash FROM app_user").fetchone()[0] == h1


def test_resend_verification_uniform(client, client2):
    client.register("r@example.com")
    a = client.post("/auth/resend-verification", {"email": "R@example.com"})
    b = client2.post("/auth/resend-verification", {"email": "nobody@example.com"})
    assert a.status_code == b.status_code == 202 and a.json() == b.json()
    assert [k for k, _, _ in client.mailer.outbox] == ["verify", "verify"]
    assert client.verify("r@example.com").status_code == 200
    client.post("/auth/resend-verification", {"email": "r@example.com"})  # verified: silent no-op
    assert len(client.mailer.outbox) == 2


def test_password_reset_flow_revokes_sessions(client, client2):
    client.register("p@example.com")
    client.verify("p@example.com")
    assert client.login("p@example.com").status_code == 200
    assert client.get("/auth/me").status_code == 200
    r = client2.post("/auth/forgot", {"email": "P@example.com"})
    assert r.status_code == 202
    assert client2.post("/auth/forgot", {"email": "ghost@example.com"}).json() == r.json()
    kind, _, tok = client2.mailer.outbox[-1]
    assert kind == "reset"
    assert client2.post("/auth/reset", {"token": tok, "password": "password1234"}).status_code == 422
    assert client2.post("/auth/reset", {"token": tok, "password": "brand new horse staple"}).status_code == 200
    assert client2.post("/auth/reset", {"token": tok, "password": "another new horse staple"}).status_code == 400
    assert client.get("/auth/me").status_code == 401  # old session dead
    assert client2.login("p@example.com", "brand new horse staple").status_code == 200
    assert client2.login("p@example.com").status_code == 401


def test_login_email_normalised_like_register(client):
    assert client.register("Ａlice@example.com").status_code == 202
    client.verify("alice@example.com")
    assert client.login("Ａlice@example.com").status_code == 200


def test_password_equal_to_email_local_part_rejected(client):
    assert client.register("longlocalpart@example.com", pw="longlocalpart").status_code == 422
    assert client.register("Mixed.Name99@example.com", pw="mixed.name99").status_code == 422


@pytest.mark.parametrize("pw", ["short", "a" * 129, "1234567890", "aaaaaaaaaaaa", "password123", "Password123!", "qwerty123456"])
def test_password_policy(client, su, pw):
    r = client.register(pw=pw)
    assert r.status_code == 422
    assert su.execute("SELECT count(*) FROM app_user").fetchone()[0] == 0


@pytest.mark.parametrize(
    "email", ["nope", "a@b", "a b@example.com", "a​@example.com", "x" * 250 + "@e.com", ""]
)
def test_register_bad_email(client, email):
    assert client.register(email=email).status_code == 422


def test_register_rejects_extra_fields(client):
    r = client.post(
        "/auth/register",
        {"email": "a@example.com", "password": PW, "accepted_terms_version": TERMS, "tier": "paid"},
    )
    assert r.status_code == 422


# -------------------------------------------------------------- verify
def test_verify_single_use(client, su):
    client.register()
    assert client.verify("a@example.com").status_code == 200
    assert su.execute("SELECT email_verified_at IS NOT NULL FROM app_user").fetchone()[0] is True
    again = client.verify("a@example.com")
    assert again.status_code == 400 and again.json()["detail"] == "invalid_or_expired_token"


def test_verify_expired(client, su):
    client.register()
    su.execute("UPDATE auth_token SET expires_at = now() - interval '1 second'")
    assert client.verify("a@example.com").status_code == 400
    assert su.execute("SELECT email_verified_at FROM app_user").fetchone()[0] is None


def test_verify_garbage_token(client):
    assert client.post("/auth/verify", {"token": "x" * 40}).status_code == 400


def test_edu_upgrade_only_for_verified_edu(client, client2, su):
    client.register("s@uni.edu")
    client2.register("p@example.com")
    assert su.execute("SELECT is_edu, tier FROM app_user WHERE email_lower='s@uni.edu'").fetchone() == (
        False,
        "free",
    )
    client.verify("s@uni.edu")
    client2.verify("p@example.com")
    assert su.execute("SELECT is_edu, tier FROM app_user WHERE email_lower='s@uni.edu'").fetchone() == (
        True,
        "edu",
    )
    assert su.execute("SELECT is_edu, tier FROM app_user WHERE email_lower='p@example.com'").fetchone() == (
        False,
        "free",
    )


def test_edu_lookalikes_not_upgraded(client, su):
    client.register("x@notedu.com")
    client.verify("x@notedu.com")
    client.register("y@edu.example.com")
    client.verify("y@edu.example.com")
    assert su.execute("SELECT count(*) FROM app_user WHERE is_edu").fetchone()[0] == 0


# --------------------------------------------------------------- login
def test_unverified_cannot_login(client):
    client.register()
    r = client.login()
    assert r.status_code == 403 and r.json()["detail"] == "email_not_verified"
    assert COOKIE_SESSION not in client.http.cookies


def test_login_uniform_errors(client):
    client.signup_login()
    wrong_pw = client.login(pw="wrong password here")
    unknown = client.login(email="ghost@example.com")
    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json() == unknown.json()


def test_login_cookie_flags_and_session(client):
    client.signup_login()
    c = client.http.cookies
    assert COOKIE_SESSION in c
    r = client.login()
    sc = [v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith(COOKIE_SESSION)][
        0
    ].lower()
    assert "httponly" in sc and "secure" in sc and "samesite=lax" in sc and "max-age=3600" in sc
    claims = jwt.decode(client.http.cookies[COOKIE_SESSION], jwt_key(), algorithms=["HS256"], issuer="metis")
    assert claims["jti"] and claims["exp"] - claims["iat"] == 3600
    assert client.get("/_whoami").status_code == 200


def test_login_timing_equalised_by_dummy_hash(client, monkeypatch):
    from backend.accounts import security

    calls = []
    monkeypatch.setattr(security, "burn_password_check", lambda pw, cost: calls.append(cost))
    client.login(email="ghost@example.com")
    assert calls == [4]


def test_lockout_after_five_and_locked_user_cannot_login(client, su):
    uid = client.signup_login()
    client.http.cookies.clear()
    for _ in range(5):
        assert client.login(pw="wrong password here").status_code == 401
    assert su.execute("SELECT locked_until > now() FROM app_user WHERE id=%s", (uid,)).fetchone()[0] is True
    r = client.login()  # right password, still locked, same uniform error
    assert r.status_code == 401 and r.json()["detail"] == "invalid_credentials"
    assert COOKIE_SESSION not in client.http.cookies
    su.execute("UPDATE app_user SET locked_until = now() - interval '1 second'")
    assert client.login().status_code == 200


# ----------------------------------------------------------------- JWT
def _set_session(client, tok):
    client.http.cookies.clear()
    client.csrf()
    client.http.cookies.set(COOKIE_SESSION, tok, domain="testserver.local")


def test_no_cookie_401(client):
    assert client.get("/_whoami").status_code == 401


def test_tampered_token_rejected(client):
    client.signup_login()
    tok = client.http.cookies[COOKIE_SESSION]
    head, body, sig = tok.split(".")
    bad = ".".join([head, body, sig[:-2] + ("AA" if not sig.endswith("AA") else "BB")])
    _set_session(client, bad)
    assert client.get("/_whoami").status_code == 401


def test_wrong_key_expired_alg_none_missing_claims(client, forge):
    uid = client.signup_login()
    cases = {
        "wrongkey": forge({"sub": str(uid)}, key=b"x" * 32),
        "expired": forge({"sub": str(uid), "exp": int(time.time()) - 5}),
        "badiss": forge({"sub": str(uid), "iss": "evil"}),
        "nojti": forge({"sub": str(uid)}, drop=("jti",)),
        "badsub": forge({"sub": "not-a-uuid"}),
        "hs512": forge({"sub": str(uid)}, alg="HS512"),
        "none": jwt.encode(
            {
                "sub": str(uid),
                "jti": str(uuid.uuid4()),
                "iat": 1,
                "exp": int(time.time()) + 600,
                "iss": "metis",
            },
            None,
            algorithm="none",
        ),
    }
    for name, tok in cases.items():
        _set_session(client, tok)
        assert client.get("/_whoami").status_code == 401, name
    _set_session(client, forge({"sub": str(uid)}))  # correctly signed but unknown jti is fine; control case
    assert client.get("/_whoami").status_code == 200


def test_token_for_unknown_or_deleted_user_rejected(client, forge):
    _set_session(client, forge())
    assert client.get("/_whoami").status_code == 401


def test_logout_revokes_token(client, su):
    client.signup_login()
    tok = client.http.cookies[COOKIE_SESSION]
    assert client.get("/_whoami").status_code == 200
    assert client.post("/auth/logout").status_code == 200
    assert su.execute("SELECT count(*) FROM session_revocation").fetchone()[0] == 1
    _set_session(client, tok)  # replay the old cookie
    assert client.get("/_whoami").status_code == 401


def test_logout_without_session_is_idempotent(client):
    assert client.post("/auth/logout").status_code == 200


# ---------------------------------------------------------------- CSRF
def test_csrf_missing_is_403_on_every_mutating_route(client):
    client.signup_login()
    for method, url, body in [
        (
            "post",
            "/auth/register",
            {"email": "z@example.com", "password": PW, "accepted_terms_version": TERMS},
        ),
        ("post", "/auth/verify", {"token": "x" * 20}),
        ("post", "/auth/login", {"email": "a@example.com", "password": PW}),
        ("post", "/auth/logout", None),
        ("post", "/consents", {"scope": "analytics", "granted": True}),
        ("post", "/terms/accept", {"version": TERMS}),
        ("delete", "/account/data", {"password": PW}),
        ("post", "/_mutate", None),
    ]:
        r = client.http.request(method.upper(), url, json=body)
        assert r.status_code == 403 and r.json()["detail"] == "csrf_failed", url


def test_csrf_mismatch_and_forged_rejected(client):
    client.signup_login()
    r = client.http.post("/_mutate", headers={CSRF_HEADER: "forged.value"})
    assert r.status_code == 403
    cookie = client.http.cookies["metis_csrf"]
    nonce = cookie.split(".")[0]
    client.http.cookies.set("metis_csrf", f"{nonce}.{'0' * 32}", domain="testserver.local")
    r = client.http.post("/_mutate", headers={CSRF_HEADER: f"{nonce}.{'0' * 32}"})
    assert r.status_code == 403  # header equals cookie but signature is not ours
    assert client.get("/_whoami").status_code == 200  # safe methods need no CSRF


# --------------------------------------------------------- rate limits
def test_rate_limit_per_ip_register(client, cfg):
    cfg.rl_register_ip = (2, 3600)
    codes = [client.register(f"u{i}@example.com").status_code for i in range(4)]
    assert codes == [202, 202, 429, 429]


def test_rate_limit_per_email_login_and_no_raw_keys_stored(client, cfg, su):
    cfg.rl_login_email = (3, 900)
    codes = [client.login(email="victim@example.com").status_code for _ in range(5)]
    assert codes[:3] == [401] * 3 and codes[3:] == [429, 429]
    r = client.login(email="victim@example.com")
    assert "retry-after" in r.headers
    keys = [k for (k,) in su.execute("SELECT key_hash FROM rate_limit_bucket")]
    assert keys and all(len(k) == 64 for k in keys)
    assert not any("victim" in k or "127.0.0.1" in k for k in keys)
    # the hash is peppered: not a plain sha256 of the raw email
    import hashlib

    assert hashlib.sha256(b"victim@example.com").hexdigest() not in keys


def test_rate_limit_verify_per_ip(client, cfg):
    cfg.rl_verify_ip = (2, 900)
    codes = [client.post("/auth/verify", {"token": "y" * 30}).status_code for _ in range(4)]
    assert codes == [400, 400, 429, 429]


def test_rate_limit_is_per_route(client, cfg):
    cfg.rl_login_ip = (1, 300)
    assert client.login(email="q@example.com").status_code == 401
    assert client.login(email="q@example.com").status_code == 429
    assert client.register().status_code == 202  # different route, own bucket


def test_xff_ignored_unless_trusted(client, cfg, su):
    cfg.rl_login_ip = (1, 300)
    h = {"X-Forwarded-For": "9.9.9.9", CSRF_HEADER: client.csrf()}
    body = {"email": "q@example.com", "password": PW}
    assert client.http.post("/auth/login", json=body, headers=h).status_code == 401
    h["X-Forwarded-For"] = "8.8.8.8"  # spoofed rotation must not escape the limit
    assert client.http.post("/auth/login", json=body, headers=h).status_code == 429


# ----------------------------------------------- wiring / config helpers
def test_lazy_wiring_and_proxy_ip(monkeypatch):
    from fastapi import FastAPI
    from starlette.requests import Request

    from backend.accounts import deps
    from backend.accounts.config import AccountsConfig
    from backend.accounts.mailer import ConsoleMailer

    app = FastAPI()
    scope = {
        "type": "http",
        "app": app,
        "headers": [(b"x-forwarded-for", b"1.1.1.1, 2.2.2.2")],
        "client": ("3.3.3.3", 1),
    }
    req = Request(scope)
    cfg = deps.get_config(req)
    assert isinstance(cfg, AccountsConfig) and cfg.cookie_secure is True and deps.get_config(req) is cfg
    assert isinstance(deps.get_mailer(req), ConsoleMailer)
    assert deps.client_ip(req, cfg) == "3.3.3.3"
    assert deps.client_ip(req, AccountsConfig(trust_proxy=True)) == "2.2.2.2"
    req2 = Request({"type": "http", "app": app, "headers": [], "client": None})
    assert deps.client_ip(req2, AccountsConfig()) == "unknown"
    monkeypatch.setenv("METIS_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/none")
    from backend.core.settings import get_settings

    get_settings.cache_clear()
    try:
        assert deps.get_factory(req) is deps.get_factory(req)
    finally:
        get_settings.cache_clear()


def test_missing_secret_key_fails_closed(monkeypatch):
    from backend.accounts import config
    from backend.core.settings import get_settings

    monkeypatch.delenv("METIS_SECRET_KEY")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError):
            config.jwt_key()
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_security_helpers_edge_cases():
    from backend.accounts import security

    assert not security.check_password("x", "not-a-bcrypt-hash")
    assert security.csrf_valid(None, None) is False
    assert security.password_problem("a" * 129, "e@x.com")
    assert security.password_problem("e@example.com", "e@example.com")
