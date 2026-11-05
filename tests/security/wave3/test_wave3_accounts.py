"""Wave 3 red-team (Agent 10), accounts: change-password, terms status/accept and the terms gate, GET /account,
forgot/reset/resend, session invalidation, CSRF and rate limits on the new routes, and enumeration.

xfail(strict=True) = real defect (reason names the finding); unmarked = protection that holds. See private/qa_wave3.md.
"""
from __future__ import annotations

import uuid

import pytest
import w2stack as S
from w2stack import Cl
from w3util import HI

from backend.accounts.config import COOKIE_CSRF, COOKIE_SESSION, CSRF_HEADER, AccountsConfig

NEW_PW = "another long passphrase 42"
EMAIL = "alice@example.com"


def defect(fid, sev, msg):
    return pytest.mark.xfail(strict=True, reason=f"{fid} {sev}: {msg}")


def change(c, cur=S.PW, new=NEW_PW):
    return c.post("/auth/change-password", {"current_password": cur, "new_password": new})


def forgot_token(c, app, email):
    assert c.post("/auth/forgot", {"email": email}).status_code == 202
    return [t for k, e, t in app.state.mailer.outbox if k == "reset" and e == email][-1]


def stale_client(app_new, c):
    """The same browser (cookies) talking to a deployment whose required terms version is newer."""
    d = Cl(app_new)
    d.http.cookies.update(c.http.cookies)
    return d


# ===================================================================================== change-password
class TestChangePassword:
    def test_requires_a_session_and_csrf(self, mk3):
        app = mk3()
        anon = Cl(app)
        assert change(anon).status_code == 401
        c = Cl(app)
        c.signup(EMAIL)
        body = {"current_password": S.PW, "new_password": NEW_PW}
        assert c.http.post("/auth/change-password", json=body).status_code == 403                       # no header
        assert c.http.post("/auth/change-password", json=body, headers={CSRF_HEADER: "a.b.c"}).status_code == 403
        assert Cl(app).login(EMAIL, S.PW).status_code == 200                                          # nothing changed

    def test_csrf_token_of_another_session_is_refused(self, mk3):
        app = mk3()
        a, b = Cl(app), Cl(app)
        a.signup(EMAIL)
        b.signup("bob@example.com")
        # bob's valid (cookie, header) pair, replayed on alice's session: bound to bob's jti, not alice's
        a.http.cookies.set(COOKIE_CSRF, b.http.cookies.get(COOKIE_CSRF))
        r = a.http.post("/auth/change-password", json={"current_password": S.PW, "new_password": NEW_PW}, headers={CSRF_HEADER: b.http.cookies.get(COOKIE_CSRF)})
        assert r.status_code == 403

    def test_other_sessions_and_stolen_cookies_die_this_one_continues(self, mk3, su3):
        app = mk3()
        c1, c2 = Cl(app), Cl(app)
        c1.signup(EMAIL)
        assert c2.login(EMAIL).status_code == 200
        stolen = c1.http.cookies.get(COOKIE_SESSION)
        r = change(c1)
        assert r.status_code == 200 and r.json()["status"] == "password_changed" and r.json()["csrf_token"]
        assert c1.get("/auth/me").status_code == 200                       # re-issued cookie
        assert c1.http.cookies.get(COOKIE_SESSION) != stolen
        assert c2.get("/auth/me").status_code == 401                       # other browser signed out
        thief = Cl(app)
        thief.http.cookies.set(COOKIE_SESSION, stolen)
        assert thief.get("/auth/me").status_code == 401                    # the pre-change cookie is dead (revoked + pwv)
        assert c1.post("/projects", {"name": "after"}).status_code == 201   # new csrf works on the continuing session
        assert Cl(app).login(EMAIL, S.PW).status_code == 401 and Cl(app).login(EMAIL, NEW_PW).status_code == 200
        assert su3.execute("select count(*) from session_revocation").fetchone()[0] >= 1
        rows = su3.execute("select action, resource, detail::text from audit_log where action = 'password_change'").fetchall()
        assert len(rows) == 1 and S.PW not in str(rows) and NEW_PW not in str(rows) and EMAIL not in str(rows)

    def test_cookie_flags_on_the_reissued_session(self, mk3):
        app = mk3()
        c = Cl(app)
        c.signup(EMAIL)
        hs = [h.lower() for h in change(c).headers.get_list("set-cookie")]
        sess = next(h for h in hs if h.startswith(COOKIE_SESSION))
        assert "httponly" in sess and "secure" in sess and "samesite=lax" in sess and "domain=" not in sess and "path=/" in sess

    @pytest.mark.parametrize("cur,new,status", [("wrong wrong wrong", NEW_PW, 403), (S.PW, S.PW, 422), (S.PW, "short", 422), (S.PW, "password1234567", 422),
                                                (S.PW, "x" * 129, 422), (S.PW, EMAIL, 422), (S.PW, "alice", 422)])
    def test_wrong_current_and_policy(self, mk3, cur, new, status):
        app = mk3()
        c = Cl(app)
        c.signup(EMAIL)
        assert change(c, cur, new).status_code == status
        assert Cl(app).login(EMAIL, S.PW).status_code == 200            # a refusal changes nothing

    def test_extra_or_missing_fields_are_rejected(self, mk3):
        c = Cl(mk3())
        c.signup(EMAIL)
        assert c.post("/auth/change-password", {"current_password": S.PW, "new_password": NEW_PW, "email": "x@y.zz"}).status_code == 422
        assert c.post("/auth/change-password", {"new_password": NEW_PW}).status_code == 422
        assert c.post("/auth/change-password", {"current_password": None, "new_password": NEW_PW}).status_code == 422

    def test_rate_limited_per_user_so_a_stolen_session_cannot_guess_the_password(self, mk3):
        app = mk3(rl_change_password_user=(3, 900))
        c, other = Cl(app), Cl(app)
        c.signup(EMAIL)
        other.signup("bob@example.com")
        codes = [change(c, "wrong wrong wrong").status_code for _ in range(5)]
        assert codes == [403, 403, 403, 429, 429]
        assert change(c, "wrong wrong wrong").headers.get("retry-after")
        assert change(c).status_code == 429                                    # even the right password is refused while limited
        assert change(other).status_code == 200                                # another user is unaffected (per-user key)

    def test_default_limit_is_small_enough(self, mk3):
        assert AccountsConfig().rl_change_password_user[0] <= 5

    def test_a_logged_out_session_cannot_change_the_password(self, mk3):
        app = mk3()
        c = Cl(app)
        c.signup(EMAIL)
        tok = c.http.cookies.get(COOKIE_SESSION)
        csrf = c.http.cookies.get(COOKIE_CSRF)
        c.post("/auth/logout")
        d = Cl(app)
        d.http.cookies.set(COOKIE_SESSION, tok)
        d.http.cookies.set(COOKIE_CSRF, csrf)
        r = d.http.post("/auth/change-password", json={"current_password": S.PW, "new_password": NEW_PW}, headers={CSRF_HEADER: csrf})
        assert r.status_code in (401, 403)
        assert Cl(app).login(EMAIL, S.PW).status_code == 200


# ============================================================================================ terms gate
MUTATING = [("POST", "/projects", {"name": "x"}), ("POST", "/consents", {"scope": "analytics", "granted": True}),
            ("POST", "/projects/{pid}/compounds", {"smiles": ["CCO"]}), ("POST", "/projects/{pid}/predict", {"endpoints": ["logd"], "ensemble_n": 5}),
            ("PUT", "/projects/{pid}/ranking", {"weights": [{"endpoint": "logd", "weight": 1, "higher_is_better": True}], "limits": [], "lam": 0.25}),
            ("POST", "/projects/{pid}/measurements", {"compound_id": str(uuid.uuid4()), "endpoint": "logd", "value": 1.0}),
            ("DELETE", "/projects/{pid}", None), ("POST", "/dockprep/ligands", {}), ("POST", "/dockprep/proteins", {}), ("POST", "/dockprep/sites", {})]
ALLOWED_WHILE_STALE = [("GET", "/auth/me"), ("GET", "/account"), ("GET", "/auth/terms/status"), ("GET", "/auth/terms"), ("POST", "/auth/logout")]


class TestTermsGate:
    def _stale(self, mk3, w3):
        app = mk3()
        c = Cl(app)
        c.signup(EMAIL)
        pid = c.post("/projects", {"name": "mine"}).json()["id"]
        new = S.build_app(w3, None, None, AccountsConfig(**{**HI, "terms_version": "2026-02"}))
        return app, c, stale_client(new, c), pid

    def test_every_mutating_domain_route_refuses_until_reaccepted(self, mk3, w3):
        _, c, d, pid = self._stale(mk3, w3)
        for method, path, body in MUTATING:
            r = d.req(method, path.format(pid=pid), json=body)
            assert r.status_code == 403 and r.json()["detail"] == "terms_reacceptance_required", (method, path, r.status_code, r.text[:100])

    def test_account_routes_stay_reachable_so_the_user_can_accept_leave_or_delete(self, mk3, w3):
        _, c, d, _ = self._stale(mk3, w3)
        for method, path in ALLOWED_WHILE_STALE[:-1]:
            assert d.get(path).status_code == 200, path
        st = d.get("/auth/terms/status").json()
        assert st["reaccept_required"] is True and st["required_version"] == "2026-02" and set(st["accepted_versions"].values()) == {"2026-01"}
        assert d.get("/auth/me").json()["terms_reaccept_required"] is True
        assert d.get("/account").json()["terms"]["reaccept_required"] is True
        assert d.post("/auth/logout").status_code == 200

    def test_account_deletion_is_not_blocked_by_stale_terms(self, mk3, w3):
        _, c, d, _ = self._stale(mk3, w3)
        assert d.delete("/account/data", {"password": S.PW}).status_code == 200

    def test_login_reports_the_need_to_reaccept_and_accept_unlocks(self, mk3, w3, su3):
        app, c, d, pid = self._stale(mk3, w3)
        lg = Cl(d.app).login(EMAIL, S.PW).json()
        assert lg["terms_reaccept_required"] is True
        assert d.post("/auth/terms/accept", {"version": "2026-01"}).status_code == 400               # only the CURRENT version is accepted
        assert d.post("/auth/terms/accept", {"version": "2026-02", "x": 1}).status_code == 422
        assert d.post("/projects", {"name": "x"}).status_code == 403
        before = su3.execute("select count(*) from terms_acceptance").fetchone()[0]
        assert d.post("/auth/terms/accept", {"version": "2026-02"}).json() == {"status": "accepted", "version": "2026-02"}
        assert su3.execute("select count(*) from terms_acceptance").fetchone()[0] == before + 3
        assert d.post("/projects", {"name": "x"}).status_code == 201
        assert d.get("/auth/terms/status").json()["reaccept_required"] is False
        assert d.post("/terms/accept", {"version": "2026-02"}).status_code == 200                    # the old path still works

    def test_accept_needs_session_csrf_and_cannot_accept_for_someone_else(self, mk3, w3):
        app, c, d, _ = self._stale(mk3, w3)
        anon = Cl(d.app)
        assert anon.post("/auth/terms/accept", {"version": "2026-02"}).status_code == 401
        assert d.http.post("/auth/terms/accept", json={"version": "2026-02"}).status_code == 403
        assert d.get("/auth/terms/status").json()["reaccept_required"] is True
        other = Cl(app)
        other.signup("bob@example.com")
        assert Cl(d.app).login("bob@example.com", S.PW).json()["terms_reaccept_required"] is True      # bob's state is his own

    def test_reads_are_also_refused_until_reaccepted(self, mk3, w3):
        _, c, d, pid = self._stale(mk3, w3)
        assert d.get("/projects").status_code == 403
        assert d.get("/account/data").status_code == 403

    def test_terms_accept_is_rate_limited(self, mk3, w3):
        _, c, d, _ = self._stale(mk3, w3)
        codes = {d.post("/auth/terms/accept", {"version": "2026-02"}).status_code for _ in range(40)}
        assert 429 in codes


# =============================================================================================== GET /account
class TestAccountSummary:
    def test_requires_auth_and_leaks_no_credentials_or_email(self, mk3):
        app = mk3()
        assert Cl(app).get("/account").status_code == 401
        c = Cl(app)
        c.signup(EMAIL)
        r = c.get("/account")
        assert r.status_code == 200
        txt = r.text.lower()
        assert EMAIL not in txt and "password" not in txt and "hash" not in txt and "$2b$" not in txt
        j = r.json()
        assert j["tier"] == "free" and j["caps"]["max_ensemble_n"] == 10 and j["caps"]["allowed_ensemble_sizes"] == [3, 5, 10]
        assert j["caps"]["max_compounds_per_project"] == 10 and j["email_verified"] is True and j["is_edu"] is False
        assert set(j["consents"].values()) == {False}

    def test_edu_address_unlocks_the_larger_sizes_only_after_verification(self, mk3):
        app = mk3()
        c = Cl(app)
        c.signup("student@mit.edu")
        j = c.get("/account").json()
        assert j["tier"] == "edu" and j["caps"]["allowed_ensemble_sizes"] == [3, 5, 10, 20] and j["is_edu"] is True
        d = Cl(app)
        assert d.register("fake@evil.example.edu.attacker.com").status_code == 202 and d.verify("fake@evil.example.edu.attacker.com").status_code == 200
        assert d.login("fake@evil.example.edu.attacker.com").json()["user"]["tier"] == "free"

    def test_each_user_sees_only_their_own_consents(self, mk3):
        app = mk3()
        a, b = Cl(app), Cl(app)
        a.signup(EMAIL)
        b.signup("bob@example.com")
        a.post("/consents", {"scope": "analytics", "granted": True})
        assert a.get("/account").json()["consents"]["analytics"] is True
        assert b.get("/account").json()["consents"]["analytics"] is False
        assert b.get("/account?user=" + str(a.uid)).json()["consents"]["analytics"] is False       # no id parameter exists to abuse


# ================================================================ forgot / reset / resend: enumeration, abuse
class TestForgotResetResend:
    def test_forgot_and_resend_answers_are_identical_for_known_unverified_and_unknown(self, mk3):
        app = mk3()
        c = Cl(app)
        c.signup(EMAIL)
        un = Cl(app)
        un.register("unverified@example.com")
        res = {}
        for name, mail in (("verified", EMAIL), ("unverified", "unverified@example.com"), ("unknown", "ghost@example.com")):
            for route in ("/auth/forgot", "/auth/resend-verification"):
                r = Cl(app).post(route, {"email": mail})
                res[(route, name)] = (r.status_code, r.json(), sorted(k for k in r.headers if k.lower() not in {"date", "content-length", "set-cookie"}))
        for route in ("/auth/forgot", "/auth/resend-verification"):
            assert len({str(res[(route, n)]) for n in ("verified", "unverified", "unknown")}) == 1, route
        kinds = [(k, e) for k, e, _ in app.state.mailer.outbox]
        assert ("reset", EMAIL) in kinds and ("reset", "unverified@example.com") not in kinds and ("reset", "ghost@example.com") not in kinds
        assert kinds.count(("verify", "unverified@example.com")) == 2 and ("verify", "ghost@example.com") not in kinds
        assert kinds.count(("verify", EMAIL)) == 1                                       # a verified address gets no new verification mail

    def test_rate_limit_answers_are_uniform_for_known_and_unknown_addresses(self, mk3):
        app = mk3(rl_forgot_email=(2, 3600), rl_resend_email=(2, 3600))
        c = Cl(app)
        c.signup(EMAIL)
        for route in ("/auth/forgot", "/auth/resend-verification"):
            known = [Cl(app).post(route, {"email": EMAIL}).status_code for _ in range(4)]
            unknown = [Cl(app).post(route, {"email": "ghost@example.com"}).status_code for _ in range(4)]
            assert known == unknown == [202, 202, 429, 429], route

    def test_email_case_and_whitespace_variants_share_one_limit_bucket(self, mk3):
        app = mk3(rl_forgot_email=(2, 3600))
        codes = [Cl(app).post("/auth/forgot", {"email": e}).status_code for e in ("A@Example.com", " a@example.com", "a@example.COM", "a@example.com")]
        assert codes == [202, 202, 429, 429]

    def test_reset_signs_out_every_session_unlocks_and_the_token_is_single_use(self, mk3, su3):
        app = mk3()
        c = Cl(app)
        c.signup(EMAIL)
        for _ in range(5):
            Cl(app).login(EMAIL, "wrong wrong wrong")
        assert Cl(app).login(EMAIL, S.PW).status_code == 401                         # locked: even the right password fails
        tok = forgot_token(Cl(app), app, EMAIL)
        r = Cl(app).post("/auth/reset", {"token": tok, "password": NEW_PW})
        assert r.status_code == 200
        assert c.get("/auth/me").status_code == 401                                   # the session from before the reset is dead
        assert Cl(app).login(EMAIL, S.PW).status_code == 401 and Cl(app).login(EMAIL, NEW_PW).status_code == 200
        assert Cl(app).post("/auth/reset", {"token": tok, "password": "yet another long passphrase 7"}).status_code == 400
        assert su3.execute("select count(*) from audit_log where action = 'password_reset'").fetchone()[0] == 1

    def test_a_weak_password_does_not_spend_the_token(self, mk3):
        app = mk3()
        Cl(app).signup(EMAIL)
        tok = forgot_token(Cl(app), app, EMAIL)
        for bad in ("short", "password1234567", "alice", EMAIL):
            assert Cl(app).post("/auth/reset", {"token": tok, "password": bad}).status_code == 422
        assert Cl(app).post("/auth/reset", {"token": tok, "password": NEW_PW}).status_code == 200

    def test_token_purposes_cannot_be_swapped(self, mk3):
        app = mk3()
        c = Cl(app)
        c.signup(EMAIL)
        reset_tok = forgot_token(Cl(app), app, EMAIL)
        un = Cl(app)
        un.register("new@example.com")
        verify_tok = un.token_for("new@example.com")
        assert Cl(app).post("/auth/verify", {"token": reset_tok}).status_code == 400
        assert Cl(app).post("/auth/reset", {"token": verify_tok, "password": NEW_PW}).status_code == 400
        assert Cl(app).login("new@example.com", S.PW).status_code == 403                            # still unverified
        assert un.verify("new@example.com").status_code == 200                                      # the verify token was not spent

    def test_expired_and_unknown_reset_tokens_say_nothing(self, mk3, su3):
        app = mk3()
        Cl(app).signup(EMAIL)
        tok = forgot_token(Cl(app), app, EMAIL)
        su3.execute("update auth_token set expires_at = now() - interval '1 second'")
        a = Cl(app).post("/auth/reset", {"token": tok, "password": NEW_PW})
        b = Cl(app).post("/auth/reset", {"token": "x" * 43, "password": NEW_PW})
        assert (a.status_code, a.json()) == (b.status_code, b.json()) == (400, {"detail": "invalid_or_expired_token"})

    def test_unverified_account_cannot_be_reset_into_a_session(self, mk3, su3):
        app = mk3()
        Cl(app).register("new@example.com")
        # a reset token for an unverified account (minted by hand) is refused and does not verify the address
        uid = su3.execute("select id from app_user where email_lower = 'new@example.com'").fetchone()[0]
        su3.execute("select auth_issue_token(%s, 'reset_password', %s, interval '1 hour')", (uid, __import__("backend.accounts.security", fromlist=["x"]).token_hash("t" * 43)))
        assert Cl(app).post("/auth/reset", {"token": "t" * 43, "password": NEW_PW}).status_code == 400
        assert Cl(app).login("new@example.com", NEW_PW).status_code in (401, 403)

    def test_all_new_public_posts_demand_csrf(self, mk3):
        app = mk3()
        c = Cl(app)
        for path, body in (("/auth/forgot", {"email": EMAIL}), ("/auth/resend-verification", {"email": EMAIL}), ("/auth/reset", {"token": "x" * 20, "password": NEW_PW})):
            assert c.http.post(path, json=body).status_code == 403, path
            assert c.http.post(path, json=body, headers={CSRF_HEADER: "a.b.c"}, cookies={COOKIE_CSRF: "a.b.c"}).status_code == 403, path

    def test_per_ip_limits_apply_to_reset_and_resend(self, mk3):
        app = mk3(rl_reset_ip=(3, 900), rl_resend_ip=(3, 3600), rl_forgot_ip=(3, 3600))
        for route, body in (("/auth/reset", {"token": "x" * 20, "password": NEW_PW}), ("/auth/resend-verification", {"email": "a@example.com"}), ("/auth/forgot", {"email": "a@example.com"})):
            c = Cl(app)
            codes = []
            for i in range(5):
                b = {**body, "email": f"u{i}@example.com"} if "email" in body else body
                codes.append(c.post(route, b).status_code)
            assert 429 in codes, route

    def test_a_reset_or_password_change_retires_every_other_outstanding_reset_token(self, mk3):
        app = mk3()
        c = Cl(app)
        c.signup(EMAIL)
        t1 = forgot_token(Cl(app), app, EMAIL)
        t2 = forgot_token(Cl(app), app, EMAIL)
        assert Cl(app).post("/auth/reset", {"token": t1, "password": NEW_PW}).status_code == 200
        assert Cl(app).post("/auth/reset", {"token": t2, "password": "attacker chosen passphrase 9"}).status_code == 400

    def test_password_change_retires_outstanding_reset_tokens(self, mk3):
        app = mk3()
        c = Cl(app)
        c.signup(EMAIL)
        t = forgot_token(Cl(app), app, EMAIL)
        assert change(c).status_code == 200
        assert Cl(app).post("/auth/reset", {"token": t, "password": "attacker chosen passphrase 9"}).status_code == 400


# ================================================================================ pre-hijacking / squatting
class TestSquatting:
    def test_attacker_chosen_password_does_not_survive_the_owners_verification(self, mk3):
        app = mk3()
        attacker, victim = Cl(app), Cl(app)
        assert attacker.register("victim@example.com", "attacker chosen passphrase 1").status_code == 202
        assert victim.register("victim@example.com", S.PW).status_code == 202               # owner signs up with their own password
        assert victim.verify("victim@example.com").status_code == 200
        assert Cl(app).login("victim@example.com", "attacker chosen passphrase 1").status_code == 401

    def test_owner_can_always_recover_by_mail_after_a_squat(self, mk3):
        app = mk3()
        Cl(app).register("victim@example.com", "attacker chosen passphrase 1")
        v = Cl(app)
        v.register("victim@example.com", S.PW)
        v.verify("victim@example.com")
        tok = forgot_token(Cl(app), app, "victim@example.com")
        assert Cl(app).post("/auth/reset", {"token": tok, "password": S.PW}).status_code == 200
        assert Cl(app).login("victim@example.com", "attacker chosen passphrase 1").status_code == 401
        assert Cl(app).login("victim@example.com", S.PW).status_code == 200

    def test_register_for_a_registered_address_is_indistinguishable_and_never_changes_the_hash(self, mk3, su3):
        app = mk3()
        Cl(app).signup(EMAIL)
        h = su3.execute("select password_hash from app_user").fetchone()[0]
        r_new = Cl(app).register("new@example.com")
        r_old = Cl(app).register(EMAIL, "attacker chosen passphrase 1")
        assert (r_new.status_code, r_new.json()) == (r_old.status_code, r_old.json())
        assert su3.execute("select password_hash from app_user where email_lower=%s", (EMAIL,)).fetchone()[0] == h
        assert ("exists", EMAIL, None) in app.state.mailer.outbox


# ============================================================================== mail delivery side channels
class _SlowMailer:
    """What a real SMTP mailer looks like to the request: send_* blocks for a while (the Mailer protocol is synchronous)."""

    def __init__(self, delay=0.0, fail=False):
        self.delay, self.fail, self.outbox = delay, fail, []

    def _send(self, kind, to, token=None):
        import time

        time.sleep(self.delay)
        if self.fail:
            raise ConnectionError("smtp down")
        self.outbox.append((kind, to, token))

    def send_verification(self, to, token):
        self._send("verify", to, token)

    def send_already_registered(self, to):
        self._send("exists", to)

    def send_reset(self, to, token):
        self._send("reset", to, token)


def _app_with(mk3, mailer):
    from fastapi.testclient import TestClient

    app = mk3()
    app.state.mailer = mailer
    c = Cl(app)
    c.http = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    return app, c


def test_forgot_and_resend_do_not_reveal_registration_through_timing_or_mailer_errors(mk3):
    import statistics
    import time

    app, c = _app_with(mk3, _SlowMailer(0.0))
    c.signup(EMAIL)
    app.state.mailer = _SlowMailer(0.25)

    def timed(route, mail):
        out = []
        for _ in range(3):
            t0 = time.perf_counter()
            r = Cl(app).post(route, {"email": mail})
            out.append(time.perf_counter() - t0)
            assert r.status_code == 202
        return statistics.median(out)

    for route in ("/auth/forgot",):
        assert abs(timed(route, EMAIL) - timed(route, "ghost@example.com")) < 0.1, route
    app.state.mailer = _SlowMailer(0.0, fail=True)
    cl = Cl(app)
    cl.http = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    assert cl.post("/auth/forgot", {"email": EMAIL}).status_code == cl.post("/auth/forgot", {"email": "ghost@example.com"}).status_code


def test_register_answers_are_uniform_even_when_the_mailer_is_slow_because_both_branches_send(mk3):
    import time

    app, c = _app_with(mk3, _SlowMailer(0.0))
    c.signup(EMAIL)
    app.state.mailer = _SlowMailer(0.2)
    t = []
    for mail in (EMAIL, "brand-new@example.com"):
        t0 = time.perf_counter()
        r = Cl(app).register(mail)
        t.append(time.perf_counter() - t0)
        assert r.status_code == 202
    assert abs(t[0] - t[1]) < 0.15


# ============================================================================================ route sweep
PUBLIC = {("GET", "/health"), ("GET", "/auth/csrf"), ("GET", "/auth/terms"), ("POST", "/auth/register"), ("POST", "/auth/login"), ("POST", "/auth/verify"),
          ("POST", "/auth/resend-verification"), ("POST", "/auth/forgot"), ("POST", "/auth/reset"), ("POST", "/auth/logout"), ("GET", "/models"), ("GET", "/models/{name}")}


def test_every_route_but_the_public_list_is_401_without_a_session_including_the_wave3_routes(mk3):
    app = mk3()
    c = Cl(app)
    seen = set()
    for path0, ops in app.openapi()["paths"].items():           # app.routes only lists lazily included routers
        for m in {k.upper() for k in ops} - {"HEAD", "OPTIONS"}:
            seen.add((m, path0))
            if (m, path0) in PUBLIC:
                continue
            path = path0
            for p in set(__import__("re").findall(r"\{[^}]+\}", path0)):
                path = path.replace(p, str(uuid.uuid4()))
            resp = c.req(m, path, json={} if m != "GET" else None)
            assert resp.status_code == 401, (m, path0, resp.status_code)
    for new in (("POST", "/auth/change-password"), ("GET", "/auth/terms/status"), ("POST", "/auth/terms/accept"), ("POST", "/terms/accept"), ("GET", "/account"), ("GET", "/jobs/{job_id}")):
        assert new in seen, new
    # Metis Lead Studio (Beta): the three /lead prefixes are part of the sweep above, none of them is public
    for new in (("GET", "/lead/api/health"), ("POST", "/lead/api/runs"), ("GET", "/lead/api/runs/{run_id}/events"), ("DELETE", "/lead/api/runs/{run_id}"),
                ("POST", "/lead/api/molecules/validate"), ("POST", "/lead/api/poses"), ("POST", "/lead/api/manual/preview"), ("GET", "/lead/api/data/{path}"),
                ("GET", "/lead/library/runs"), ("POST", "/lead/library/manual"), ("GET", "/lead/profiles"), ("PUT", "/lead/profiles/{pid}")):
        assert new in seen and new not in PUBLIC, new
    assert not {r for r in PUBLIC if r[1].startswith("/lead")}
    assert PUBLIC <= seen | {("GET", "/models/{name}")}


# ================================================================================================ caching
def test_authenticated_json_is_not_cacheable(mk3):
    app = mk3()
    c = Cl(app)
    c.signup(EMAIL)
    c.post("/projects", {"name": "p"})
    for path in ("/auth/me", "/projects", "/account", "/consents"):
        assert "no-store" in c.get(path).headers.get("cache-control", ""), path
