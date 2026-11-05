"""Wave 4 account hardening: pre-hijacking (F-122), terms accept (F-124), reset tokens (F-125), mail off the request
path (F-131), rate-limit windows (F-134), the email on /auth/me."""

import time

import pytest
from pydantic import ValidationError

from backend.accounts import mailer as mailer_mod
from backend.accounts.config import RATE_LIMIT_PURGE_FLOOR_S, AccountsConfig
from backend.accounts.mailer import InMemoryMailer, deliver

PW = "correct horse battery"  # same as tests/accounts/conftest.py
TERMS = "2026-01"
OWNER_PW = "the owners own passphrase"


@pytest.fixture
def C(client):
    """Factory for another browser on the same app (the accounts conftest client class: cookie jar + CSRF)."""
    return lambda app: type(client)(app)


# ---------------------------------------------------------------- F-122
def test_newest_registration_wins_and_the_squatters_link_and_password_die(client, app, su, C):
    squatter = C(app)
    assert squatter.register("victim@example.com", "squatter chosen passphrase").status_code == 202
    squat_token = client.token_for("victim@example.com")
    assert client.register("victim@example.com", OWNER_PW).status_code == 202  # the owner signs up
    assert client.post("/auth/verify", {"token": squat_token}).status_code == 400  # first link retired
    assert client.verify("victim@example.com").status_code == 200
    assert C(app).login("victim@example.com", "squatter chosen passphrase").status_code == 401
    assert C(app).login("victim@example.com", OWNER_PW).status_code == 200
    assert su.execute("SELECT count(*) FROM audit_log WHERE action = 'register_replace'").fetchone()[0] == 1


def test_a_verified_account_is_never_touched_by_a_later_registration(client, app, su, C):
    client.signup_login("done@example.com", OWNER_PW)
    h = su.execute("SELECT password_hash FROM app_user").fetchone()[0]
    r = C(app).register("done@example.com", "attacker chosen passphrase")
    assert r.status_code == 202 and r.json() == {"status": "verification_sent"}
    assert su.execute("SELECT password_hash FROM app_user").fetchone()[0] == h
    assert ("exists", "done@example.com", None) in client.mailer.outbox


def test_registering_again_gives_no_signal_that_the_address_was_known(client, app, C):
    a = C(app).register("new@example.com")
    b = C(app).register("new@example.com", "someone elses passphrase")
    assert (a.status_code, a.json()) == (b.status_code, b.json())


def test_verify_can_set_the_password_that_survives(client, app, C):
    assert client.register("o@example.com", "squatter chosen passphrase").status_code == 202
    r = client.post("/auth/verify", {"token": client.token_for("o@example.com"), "password": OWNER_PW})
    assert r.status_code == 200 and r.json() == {"status": "verified"}
    assert C(app).login("o@example.com", "squatter chosen passphrase").status_code == 401
    assert C(app).login("o@example.com", OWNER_PW).status_code == 200


def test_verify_password_is_policy_checked_and_a_refusal_does_not_spend_the_token(client):
    client.register("p@example.com")
    tok = client.token_for("p@example.com")
    for bad in ("short", "p@example.com", "password1234567", "x" * 129):
        assert client.post("/auth/verify", {"token": tok, "password": bad}).status_code == 422, bad
    assert client.post("/auth/verify", {"token": tok, "password": "p@example.com"}).status_code == 422
    assert client.login("p@example.com").status_code == 403  # still unverified: the token was not spent
    assert client.post("/auth/verify", {"token": tok, "password": OWNER_PW}).status_code == 200


def test_verify_without_password_keeps_working_unchanged(client):
    client.register("w@example.com")
    assert client.verify("w@example.com").status_code == 200
    assert client.login("w@example.com").status_code == 200
    assert client.post("/auth/verify", {"token": "x" * 20, "extra": 1}).status_code == 422


# ---------------------------------------------------------------- /auth/me
def test_me_returns_the_email_to_its_owner_only(client, app, C):
    client.signup_login("me@example.com")
    j = client.get("/auth/me").json()
    assert j["email"] == "me@example.com" and "password" not in str(j) and "hash" not in str(j)
    other = C(app)
    other.signup_login("other@example.com")
    assert other.get("/auth/me").json()["email"] == "other@example.com"
    assert C(app).get("/auth/me").status_code == 401


# ---------------------------------------------------------------- F-124
def test_terms_accept_deduplicates_and_is_rate_limited(client, app, su):
    client.signup_login()
    app.state.accounts_config = AccountsConfig(
        bcrypt_cost=4, terms_version="2026-02", cookie_secure=True, rl_terms_accept_user=(3, 900)
    )
    base = su.execute("SELECT count(*) FROM terms_acceptance").fetchone()[0]
    codes = []
    for _ in range(5):
        codes.append(client.post("/auth/terms/accept", {"version": "2026-02"}).status_code)
    assert codes == [200, 200, 200, 429, 429]
    assert su.execute("SELECT count(*) FROM terms_acceptance").fetchone()[0] == base + 3  # one set of three rows only
    assert su.execute("SELECT count(*) FROM audit_log WHERE action = 'terms_reaccept'").fetchone()[0] == 1


def test_accepting_the_version_already_held_adds_no_rows(client, su):
    client.signup_login()
    base = su.execute("SELECT count(*) FROM terms_acceptance").fetchone()[0]
    r = client.post("/terms/accept", {"version": TERMS})
    assert r.status_code == 200 and r.json() == {"status": "accepted", "version": TERMS}
    assert su.execute("SELECT count(*) FROM terms_acceptance").fetchone()[0] == base


# ---------------------------------------------------------------- F-125
def test_a_new_forgot_keeps_the_first_token_but_the_first_success_kills_the_rest(client, app):
    client.signup_login()
    client.post("/auth/forgot", {"email": "a@example.com"})
    client.post("/auth/forgot", {"email": "a@example.com"})
    t = [x[2] for x in client.mailer.outbox if x[0] == "reset"]
    assert len(t) == 2
    assert client.post("/auth/reset", {"token": t[0], "password": OWNER_PW}).status_code == 200
    assert client.post("/auth/reset", {"token": t[1], "password": "attacker chosen passphrase"}).status_code == 400


def test_password_change_retires_outstanding_reset_tokens(client):
    client.signup_login()
    client.post("/auth/forgot", {"email": "a@example.com"})
    tok = [x[2] for x in client.mailer.outbox if x[0] == "reset"][-1]
    assert client.post("/auth/change-password", {"current_password": PW, "new_password": OWNER_PW}).status_code == 200
    assert client.post("/auth/reset", {"token": tok, "password": "attacker chosen passphrase"}).status_code == 400


# ---------------------------------------------------------------- F-131
class _Blocking(InMemoryMailer):
    instant = False

    def __init__(self, delay=0.0, fail=False):
        super().__init__()
        self.delay, self.fail = delay, fail

    def send_reset(self, to, token):
        time.sleep(self.delay)
        if self.fail:
            raise ConnectionError("smtp down: " + to)
        super().send_reset(to, token)

    def send_verification(self, to, token):
        time.sleep(self.delay)
        super().send_verification(to, token)


def test_deliver_runs_slow_mailers_off_the_request_and_swallows_their_errors(caplog):
    m = _Blocking(delay=0.3)
    t0 = time.perf_counter()
    deliver(m, "send_reset", "x@example.com", "tok")
    assert time.perf_counter() - t0 < 0.15 and m.outbox == []  # returned before the mail was sent
    deadline = time.time() + 5
    while not m.outbox and time.time() < deadline:
        time.sleep(0.02)
    assert m.outbox == [("reset", "x@example.com", "tok")]
    bad = _Blocking(fail=True)
    with caplog.at_level("WARNING", logger="metis.accounts.mail"):
        deliver(bad, "send_reset", "secret@example.com", "tok")
        time.sleep(0.3)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ConnectionError" in text and "secret@example.com" not in text and "tok" not in text.replace("token", "")


def test_instant_mailers_are_called_inline():
    m = InMemoryMailer()
    deliver(m, "send_reset", "x@example.com", "tok")
    assert m.outbox == [("reset", "x@example.com", "tok")]
    assert mailer_mod.ConsoleMailer.instant is True


@pytest.mark.parametrize("route", ["/auth/forgot", "/auth/resend-verification"])
def test_forgot_and_resend_status_and_time_do_not_depend_on_the_address(client, app, route, C):
    client.signup_login()
    unv = C(app)
    unv.register("unverified@example.com")
    app.state.mailer = _Blocking(delay=0.3, fail=True)
    out = {}
    for name, mail in (("verified", "a@example.com"), ("unverified", "unverified@example.com"), ("unknown", "nobody@example.com")):
        t0 = time.perf_counter()
        r = C(app).post(route, {"email": mail})
        out[name] = (r.status_code, r.json(), time.perf_counter() - t0)
    assert len({(s, j["status"]) for s, j, _ in out.values()}) == 1 and out["verified"][0] == 202
    assert max(t for _, _, t in out.values()) < 0.25  # none of them waited for the 0.3 s (failing) mailer


# ---------------------------------------------------------------- F-134
@pytest.mark.parametrize("bad", [(10, RATE_LIMIT_PURGE_FLOOR_S + 1), (10, 86400), (10, 0), (0, 900), (-1, 900)])
def test_rate_limit_windows_above_the_purge_floor_are_refused_at_startup(bad):
    with pytest.raises(ValidationError):
        AccountsConfig(rl_login_ip=bad)


def test_every_default_window_fits_and_the_floor_matches_the_migration():
    cfg = AccountsConfig()
    for name in type(cfg).model_fields:
        if name.startswith("rl_"):
            limit, window = getattr(cfg, name)
            assert limit >= 1 and 1 <= window <= RATE_LIMIT_PURGE_FLOOR_S, name
    AccountsConfig(rl_login_ip=(5, RATE_LIMIT_PURGE_FLOOR_S))
    import pathlib

    mig = (pathlib.Path(__file__).parents[2] / "backend/db/migrations/0004_wave3_hardening.sql").read_text()
    assert "interval '2 hours'" in mig and RATE_LIMIT_PURGE_FLOOR_S == 2 * 3600
