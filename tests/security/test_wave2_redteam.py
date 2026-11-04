"""Wave 2 red-team suite (Agent 9): accounts, domain API, docking prep, ML, deploy/CI.

Convention (same as Wave 1): xfail(strict=True, reason="F-xx SEVERITY: ...") reproduces a REAL defect and turns into
a hard failure (XPASS-strict) the day it is fixed, so the marker must then be removed. Unmarked tests are
protections that hold today. Full write-up: private/qa_wave2.md. Helpers: tests/security/wave2/.
"""
import base64
import json
import logging
import pathlib
import re
import shutil
import sys
import threading
import time
import uuid

import psycopg
import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE / "wave2"))

import w2stack as S  # noqa: E402
from w2stack import Cl  # noqa: E402

from backend.accounts.config import COOKIE_CSRF, COOKIE_SESSION, CSRF_HEADER, AccountsConfig  # noqa: E402


def defect(fid, sev, msg):
    return pytest.mark.xfail(strict=True, reason=f"{fid} {sev}: {msg}")


# ------------------------------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def w2(pg):
    factory, engine = S.login_factory(pg)
    yield factory
    engine.dispose()


@pytest.fixture
def su2(pg):
    with psycopg.connect(pg, autocommit=True) as c:
        yield c
        c.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_no_update")
        c.execute("TRUNCATE app_user, model_version, deletion_request, audit_log, rate_limit_bucket CASCADE")
        c.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_no_update")


HI = dict(bcrypt_cost=4, terms_version=S.TERMS, cookie_secure=True, rl_register_ip=(10**6, 3600),
          rl_register_email=(10**6, 3600), rl_login_ip=(10**6, 300), rl_login_email=(10**6, 900))


@pytest.fixture
def mk(w2, su2):
    """Factory: (app, helper) with generous rate limits unless cfg is given."""
    from api_support import FakeRegistry

    def make(registry=None, queue=None, **cfg):
        su2.execute("INSERT INTO model_version (id, endpoint, version) VALUES (%s,'test','t0') ON CONFLICT DO NOTHING",
                    (str(FakeRegistry.version_id),))
        c = AccountsConfig(**{**HI, **cfg})
        return S.build_app(w2, registry or FakeRegistry(), queue, c)

    return make


# =========================================================================================== A. ACCOUNTS
class TestAccounts:
    def test_jwt_forgeries_rejected(self, mk):
        import time as _t

        import jwt

        from backend.accounts.config import jwt_key

        app = mk()
        c = Cl(app)
        uid = c.signup("a@example.com")
        now = int(_t.time())
        base = {"sub": str(uid), "jti": str(uuid.uuid4()), "iat": now, "exp": now + 600, "iss": "metis"}
        variants = {
            "none": jwt.encode(base, None, algorithm="none"),
            "hs512": jwt.encode(base, jwt_key(), algorithm="HS512"),
            "wrongkey": jwt.encode(base, b"x" * 32, algorithm="HS256"),
            "expired": jwt.encode({**base, "exp": now - 10}, jwt_key(), algorithm="HS256"),
            "noiss": jwt.encode({k: v for k, v in base.items() if k != "iss"}, jwt_key(), algorithm="HS256"),
            "nojti": jwt.encode({k: v for k, v in base.items() if k != "jti"}, jwt_key(), algorithm="HS256"),
            "badsub": jwt.encode({**base, "sub": "not-a-uuid"}, jwt_key(), algorithm="HS256"),
        }
        for name, tok in variants.items():
            d = Cl(app)
            d.http.cookies.set(COOKIE_SESSION, tok)
            assert d.get("/auth/me").status_code == 401, name
        ok = Cl(app)
        ok.http.cookies.set(COOKIE_SESSION, jwt.encode(base, jwt_key(), algorithm="HS256"))
        assert ok.get("/auth/me").status_code == 200  # sanity: the only valid one

    def test_logout_revokes_jti_and_cookie_flags(self, mk):
        app = mk()
        c = Cl(app)
        r = c.register("a@example.com")
        c.verify("a@example.com")
        r = c.login("a@example.com")
        sc = [h for h in r.headers.get_list("set-cookie")]
        sess = next(h for h in sc if h.startswith(COOKIE_SESSION))
        csrf = next(h for h in sc if h.startswith(COOKIE_CSRF))
        assert "httponly" in sess.lower() and "secure" in sess.lower() and "samesite=lax" in sess.lower()
        assert "httponly" not in csrf.lower() and "secure" in csrf.lower()
        tok = c.http.cookies.get(COOKIE_SESSION)
        assert c.post("/auth/logout").status_code == 200
        d = Cl(app)
        d.http.cookies.set(COOKIE_SESSION, tok)
        assert d.get("/auth/me").status_code == 401

    def test_csrf_required_everywhere_incl_prelogin(self, mk):
        app = mk()
        c = Cl(app)
        for path, body in (("/auth/register", {"email": "a@example.com", "password": S.PW, "accepted_terms_version": S.TERMS}),
                           ("/auth/login", {"email": "a@example.com", "password": S.PW}),
                           ("/auth/verify", {"token": "x" * 12})):
            assert c.http.post(path, json=body).status_code == 403
            assert c.http.post(path, json=body, headers={CSRF_HEADER: "a.b"}, cookies={COOKIE_CSRF: "a.b"}).status_code == 403

    def test_no_user_enumeration(self, mk):
        app = mk()
        c = Cl(app)
        c.signup("a@example.com")
        r1 = Cl(app).register("a@example.com")
        r2 = Cl(app).register("new@example.com")
        assert (r1.status_code, r1.json()) == (r2.status_code, r2.json())
        bodies = {Cl(app).login(e, p).text for e, p in (("a@example.com", "wrongwrongwrong"), ("ghost@example.com", "wrongwrongwrong"))}
        assert len(bodies) == 1

    def test_tokens_hashed_single_use_expiring(self, mk, su2):
        app = mk()
        c = Cl(app)
        c.register("a@example.com")
        tok = c.token_for("a@example.com")
        assert su2.execute("select count(*) from auth_token where token_hash=%s", (tok,)).fetchone()[0] == 0
        assert c.verify("a@example.com").status_code == 200
        assert c.verify("a@example.com").status_code == 400
        c.register("b@example.com")
        su2.execute("update auth_token set expires_at = now() - interval '1 second'")
        assert c.verify("b@example.com").status_code == 400

    def test_bcrypt_72_byte_truncation_not_present(self, mk):
        app = mk()
        c = Cl(app)
        p = "Abc1!xyz" * 10
        c.signup("a@example.com", p + "x")
        assert Cl(app).login("a@example.com", p + "y").status_code == 401

    def test_terms_reacceptance_blocks_mutations(self, mk, w2):
        app = mk()
        c = Cl(app)
        c.signup("a@example.com")
        app2 = S.build_app(w2, None, None, AccountsConfig(**{**HI, "terms_version": "2026-02"}))
        c2 = Cl(app2)
        c2.http.cookies.update(c.http.cookies)
        assert c2.post("/projects", {"name": "x"}).status_code == 403
        assert c2.post("/consents", {"scope": "analytics", "granted": True}).status_code == 403
        assert c2.post("/terms/accept", {"version": "2026-02"}).status_code == 200
        assert c2.post("/projects", {"name": "x"}).status_code == 201

    def test_consents_default_off_and_audited(self, mk, su2):
        app = mk()
        c = Cl(app)
        c.signup("a@example.com")
        st = c.get("/consents").json()["consents"]
        assert not any(v["granted"] for v in st.values())
        assert c.post("/consents", {"scope": "train_models", "granted": True}).json()["consents"]["train_models"]["granted"]
        assert c.post("/consents", {"scope": "bogus", "granted": True}).status_code == 422
        c.post("/consents", {"scope": "train_models", "granted": False})
        assert not c.get("/consents").json()["consents"]["train_models"]["granted"]
        assert su2.execute("select count(*) from audit_log where action in ('consent_grant','consent_revoke')").fetchone()[0] == 2

    def test_delete_requires_password_cascades_and_audit_is_pseudonymous(self, mk, su2):
        app = mk()
        c = Cl(app)
        uid = c.signup("a@example.com")
        c.post("/projects", {"name": "secret-proj"})
        assert c.delete("/account/data", {"password": "wrongwrongwrong"}).status_code == 403
        assert c.delete("/account/data", {"password": S.PW}).status_code == 200
        for t in ("app_user", "project", "consent", "terms_acceptance", "session_revocation"):
            assert su2.execute(f"select count(*) from {t}").fetchone()[0] == 0
        assert c.get("/auth/me").status_code == 401
        rows = su2.execute("select resource, detail::text from audit_log").fetchall()
        assert rows and not any("a@example.com" in str(r) or "secret-proj" in str(r) for r in rows)  # INFO: audit rows survive by design

    def test_export_does_not_leak_shared_projects(self, mk, su2):
        app = mk()
        a, b = Cl(app), Cl(app)
        ua, ub = a.signup("a@example.com"), b.signup("b@example.com")
        pid = a.post("/projects", {"name": "A-PRIVATE"}).json()["id"]
        a.post(f"/projects/{pid}/compounds", {"smiles": ["CCOCC"]})
        su2.execute("insert into grant_ (owner_id, project_id, grantee_id, permission) values (%s,%s,%s,'view')", (ua, pid, ub))
        txt = b.get("/account/data").text
        assert "A-PRIVATE" not in txt and "CCOCC" not in txt and str(ua) not in txt.replace(str(ub), "")
        assert b.get(f"/projects/{pid}").status_code == 404  # owner-only API: grantee read path is not exposed

    def test_xff_ignored_by_default_and_last_hop_when_trusted(self, mk):
        app = mk(rl_login_ip=(3, 300))
        c = Cl(app)
        codes = [c.http.post("/auth/login", json={"email": f"x{i}@example.com", "password": "wrongwrongwrong"},
                             headers={CSRF_HEADER: c.csrf(), "X-Forwarded-For": f"9.9.9.{i}"}).status_code for i in range(5)]
        assert codes[-1] == 429
        app2 = mk(rl_login_ip=(3, 300), trust_proxy=True)
        d = Cl(app2)
        codes = [d.http.post("/auth/login", json={"email": f"y{i}@example.com", "password": "wrongwrongwrong"},
                             headers={CSRF_HEADER: d.csrf(), "X-Forwarded-For": f"{i}.1.1.1, 5.5.5.5"}).status_code for i in range(5)]
        assert codes[-1] == 429  # spoofed leading hops do not buy new buckets

    def test_rate_limit_keys_hashed_and_email_case_shared(self, mk, su2):
        app = mk(rl_login_email=(2, 900))
        c = Cl(app)
        codes = [c.login(e, "wrongwrongwrong").status_code for e in ("V@Example.com", "v@example.com", " v@EXAMPLE.com ", "v@example.com")]
        assert 429 in codes
        keys = [r[0] for r in su2.execute("select key_hash from rate_limit_bucket").fetchall()]
        assert keys and all(re.fullmatch(r"[0-9a-f]{64}", k) for k in keys)

    def test_no_secrets_in_logs(self, mk, caplog):
        caplog.set_level(logging.DEBUG)
        app = mk()
        c = Cl(app)
        c.signup("leaky.person@example.com", "S3cret-pass-phrase!")
        tok = c.http.cookies.get(COOKIE_SESSION)
        c.post("/projects", {"name": "proj"})
        out = caplog.text
        for s in ("leaky.person@example.com", "S3cret-pass-phrase!", tok, c.token_for("leaky.person@example.com")):
            assert s not in out

    # ---- defects
    def test_lock_expiry_resets_counter(self, mk, su2):
        app = mk()
        Cl(app).signup("v@example.com")
        a = Cl(app)
        for _ in range(5):
            a.login("v@example.com", "wrongwrongwrong")
        su2.execute("update app_user set locked_until = now() - interval '1 minute'")
        a.login("v@example.com", "wrongwrongwrong")
        locked = su2.execute("select locked_until > now() from app_user").fetchone()[0]
        assert not locked

    def test_unverified_squat_reclaimable(self, mk, su2):
        app = mk()
        Cl(app).register("victim@example.edu")
        su2.execute("update app_user set created_at = now() - interval '3 days'")
        v = Cl(app)
        assert v.register("victim@example.edu").status_code == 202
        assert v.mailer.outbox[-1][0] == "verify"

    def test_csrf_bound_to_session(self, mk):
        app = mk()
        victim = Cl(app)
        victim.signup("a@example.com")
        tok = Cl(app).csrf()
        for ck in list(victim.http.cookies.jar):
            if ck.name == COOKIE_CSRF:
                victim.http.cookies.jar.clear(ck.domain, ck.path, ck.name)
        victim.http.cookies.set(COOKIE_CSRF, tok, domain="testserver.local")
        r = victim.http.post("/projects", json={"name": "x"}, headers={CSRF_HEADER: tok})
        assert r.status_code == 403

    @pytest.mark.parametrize("pw", ["password1234", "qwerty123456", "iloveyou1234", "Password123!"])
    def test_common_passwords_rejected(self, mk, pw):
        assert Cl(mk()).register("p@example.com", pw).status_code == 422

    def test_login_email_normalisation_matches_register(self, mk):
        app = mk()
        c = Cl(app)
        fw = "Ａlice@example.com"
        assert c.register(fw).status_code == 202
        c.http.post("/auth/verify", json={"token": c.mailer.outbox[-1][2]}, headers=c.h())
        assert c.login(fw).status_code == 200

    def test_rate_limit_buckets_purged(self):
        src = "".join(p.read_text() for p in (ROOT / "backend").rglob("*.py")) + "".join(
            p.read_text() for p in (ROOT / "scripts").rglob("*.py"))
        assert re.search(r"DELETE\s+FROM\s+rate_limit_bucket", src, re.I)

    def test_nan_json_is_422_not_500(self, mk):
        from fastapi.testclient import TestClient

        app = mk()
        c = Cl(app)
        c.signup("a@example.com")
        t = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
        t.cookies.update(c.http.cookies)
        r = t.post("/projects", content='{"name": NaN}', headers={CSRF_HEADER: c.csrf(), "content-type": "application/json"})
        assert r.status_code == 422

    def test_compose_configures_client_ip_handling(self):
        compose = (ROOT / "deploy" / "docker-compose.yml").read_text()
        assert "METIS_ACCOUNTS_TRUST_PROXY" in compose

    def test_resend_and_reset_endpoints_exist(self, mk):
        spec = mk().openapi()["paths"]
        assert any("reset" in p for p in spec) and any("resend" in p for p in spec)


# ===================================================================================== B. DOMAIN API
class TestDomain:
    def _two(self, mk, **kw):
        app = mk(**kw)
        a, b = Cl(app), Cl(app)
        a.signup("a@example.edu")
        b.signup("b@example.com")
        return app, a, b

    def test_authz_sweep_every_route(self, mk):
        app = mk()
        anon = Cl(app)
        public = {"/health", "/auth/csrf", "/auth/terms", "/auth/register", "/auth/verify", "/auth/login", "/models",
                  "/models/{name}", "/auth/logout", "/auth/resend-verification", "/auth/forgot", "/auth/reset"}
        n = 0
        for path, ops in app.openapi()["paths"].items():
            if path in public:
                continue
            url = path
            for k in ("project_id", "compound_id", "job_id", "prediction_id"):
                url = url.replace("{%s}" % k, str(uuid.uuid4()))
            for m in ops:
                r = anon.http.request(m.upper(), url, headers=anon.h(), json={} if m != "get" else None)
                assert r.status_code in (401, 403), (m, path, r.status_code)
                n += 1
        assert n >= 20

    def test_idor_everything_is_404_for_other_users(self, mk):
        app, a, b = self._two(mk)
        pid = a.post("/projects", {"name": "p"}).json()["id"]
        comps = a.post(f"/projects/{pid}/compounds", {"smiles": ["CCO", "c1ccccc1O"]}).json()["compounds"]
        cid = comps[0]["id"]
        job = a.post(f"/projects/{pid}/predict", {"endpoints": ["logd"]}).json()["job_id"]
        app.state.test_queue.wait(uuid.UUID(job))
        pred = a.get(f"/jobs/{job}").json()["prediction_ids"][0]
        base = f"/projects/{pid}"
        checks = [("get", base), ("delete", base), ("get", f"{base}/export?format=csv"), ("get", f"{base}/compounds/{cid}/conformer"),
                  ("get", f"{base}/compounds/{cid}/depiction.svg"), ("get", f"{base}/compounds/{cid}/atoms"),
                  ("get", f"/jobs/{job}"), ("get", f"/predictions/{pred}/uncertainty")]
        for m, u in checks:
            r = b.req(m.upper(), u)
            assert r.status_code == 404, (m, u, r.status_code)
        for m, u, body in (("POST", f"{base}/compounds", {"smiles": ["CCN"]}), ("POST", f"{base}/predict", {"endpoints": ["logd"]}),
                           ("POST", f"{base}/measurements", {"compound_id": cid, "endpoint": "logd", "value": 1.0}),
                           ("PUT", f"{base}/ranking", {"weights": [{"endpoint": "logd", "weight": 1, "higher_is_better": True}]})):
            assert b.req(m, u, json=body).status_code == 404, (m, u)
        assert a.get(base).status_code == 200  # untouched

    def test_mass_assignment_rejected(self, mk):
        app, a, _ = self._two(mk)
        assert a.post("/projects", {"name": "x", "owner_id": str(uuid.uuid4())}).status_code == 422
        pid = a.post("/projects", {"name": "x"}).json()["id"]
        assert a.post(f"/projects/{pid}/compounds", {"smiles": ["CCO"], "owner_id": str(uuid.uuid4())}).status_code == 422

    def test_compound_cap_race(self, mk, su2):
        app, a, _ = self._two(mk)
        pid = a.post("/projects", {"name": "x"}).json()["id"]
        out = []

        def add(i):
            c = Cl(app)
            c.http.cookies.update(a.http.cookies)
            out.append(c.post(f"/projects/{pid}/compounds", {"smiles": [("C" * (i + 1)) + "O" + "N" * j for j in range(1, 7)]}).status_code)

        ts = [threading.Thread(target=add, args=(i,)) for i in range(1, 5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert su2.execute("select count(*) from compound").fetchone()[0] <= 10
        assert set(out) <= {201, 422}

    def test_hostile_uploads_are_contained(self, mk):
        app, a, _ = self._two(mk)
        pid = a.post("/projects", {"name": "x"}).json()["id"]
        u = f"/projects/{pid}/compounds"
        cases = [
            {"format": "sdf", "content": "\n  x\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n$$$$\n" * 5000},
            {"format": "molblock", "content": "\n  x\n\n999999999  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n"},
            {"format": "csv", "content": "smiles\n" + "C" * 900000 + "\n"},
            {"format": "smiles", "smiles": ["C" + "(C" * 5000 + ")" * 5000]},
            {"format": "smiles", "smiles": ["C" * 1990]},
            {"format": "smiles", "smiles": ["<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'b'>]><x>&a;</x>"]},
            {"format": "csv", "content": "smiles\n\x00CCO\n"},
        ]
        for body in cases:
            t = time.time()
            r = a.post(u, body)
            assert r.status_code in (413, 422), (str(body)[:40], r.status_code)
            assert time.time() - t < 15
        assert a.get(f"/projects/{pid}").json()["compounds"] == []

    def test_csv_formula_and_sdf_injection_neutralised(self, mk):
        app, a, _ = self._two(mk)
        pid = a.post("/projects", {"name": "x"}).json()["id"]
        content = "smiles,name\nCCO,=HYPERLINK(\"http://x\")\nCCN,@SUM(A1)\nCCCl,+1+1\nCCBr,-2+3\nCCI,\t=1+1\n"
        assert a.post(f"/projects/{pid}/compounds", {"format": "csv", "content": content}).status_code == 201
        csv_text = a.get(f"/projects/{pid}/export?format=csv").text
        rows = [ln for ln in csv_text.splitlines() if ln and not ln.startswith("#")][1:]
        for r in rows:
            label = r.split(",", 2)[1].lstrip('"')
            assert not label[:1] in "=+-@\t" or label.startswith("'")
        sdf = a.get(f"/projects/{pid}/export?format=sdf").text
        assert sdf.count("$$$$") == 5

    def test_sdf_label_cannot_forge_records(self, mk):
        app, a, _ = self._two(mk)
        pid = a.post("/projects", {"name": "x"}).json()["id"]
        evil = "CCO,x $$$$ y\n"
        a.post(f"/projects/{pid}/compounds", {"format": "csv", "content": "smiles,name\n" + evil})
        sdf = a.get(f"/projects/{pid}/export?format=sdf").text
        assert sdf.count("$$$$") == 1 and sdf.rstrip().endswith("$$$$")

    def test_disclaimer_in_every_export(self, mk):
        app, a, _ = self._two(mk)
        from backend.exports.formats import disclaimer

        head, body = disclaimer()
        empty = a.post("/projects", {"name": "empty"}).json()["id"]
        full = a.post("/projects", {"name": "full"}).json()["id"]
        a.post(f"/projects/{full}/compounds", {"smiles": ["CCO", "c1ccccc1"]})
        for pid in (empty, full):
            assert body[:40] in a.get(f"/projects/{pid}/export?format=csv").text
            r = a.get(f"/projects/{pid}/export?format=pdf")
            assert r.status_code == 200 and r.content.startswith(b"%PDF")
        sdf = a.get(f"/projects/{full}/export?format=sdf").text
        assert sdf.count(">  <DISCLAIMER>") == sdf.count("$$$$") == 2
        assert a.get(f"/projects/{full}/export?format=pdf").content.count(b"Disclaimer") >= 1

    def test_sandbox_env_stripped_and_stdin_only(self, monkeypatch):
        import subprocess

        from backend.compounds import sandbox

        seen = {}
        real = subprocess.run

        def spy(argv, **kw):
            seen.update(kw, argv=argv)
            return real(argv, **kw)

        monkeypatch.setattr(sandbox.subprocess, "run", spy)
        assert sandbox.run({"op": "standardize", "format": "smiles", "smiles": ["CCO"], "max_records": 10})["records"][0]["ok"]
        assert seen["env"] == {} and "-I" in seen["argv"] and b"CCO" not in " ".join(map(str, seen["argv"])).encode()

    def test_cross_user_job_ids_invisible_and_queue_owner_scoped(self):
        from backend.jobs.queue import InProcessJobQueue

        q, u1, u2 = InProcessJobQueue(), uuid.uuid4(), uuid.uuid4()
        rec = q.submit(u1, uuid.uuid4(), 1, 0, lambda p: None)
        q.wait(rec.id)
        assert q.get(u1, rec.id) and q.get(u2, rec.id) is None

    # --- ranking engine properties (spec 7)
    def _preds(self, rng, n, endpoints=("logd", "ames")):
        from backend.schemas.contracts import Prediction, UncertaintyBreakdown

        bd = UncertaintyBreakdown(ensemble_disagreement=.5, similarity_to_training=.3, assay_noise=.2)
        out = {}
        for i in range(n):
            d = {}
            for e in endpoints:
                if e == "ames":
                    p = float(rng.random())
                    d[e] = Prediction(endpoint=e, kind="classification", model_version_id=uuid.uuid4(), ensemble_n=5, value=p,
                                      probability=p, coverage=.9, sample_size=10, method_note="t", breakdown=bd)
                else:
                    v = float(rng.uniform(-3, 7))
                    w = float(rng.uniform(0, 4))
                    d[e] = Prediction(endpoint=e, kind="regression", model_version_id=uuid.uuid4(), ensemble_n=5, value=v,
                                      interval_low=v - w, interval_high=v + w, coverage=.9, sample_size=10, method_note="t", breakdown=bd)
            out[f"c{i}"] = d
        return out

    def test_ranking_properties(self):
        import numpy as np

        from backend.ranking.engine import EndpointInfo, rank_compounds
        from backend.schemas.contracts import RankingConfig

        info = {"logd": EndpointInfo((-2, 6), 2.0), "ames": EndpointInfo((0, 1))}
        rng = np.random.default_rng(0)
        for trial in range(60):
            preds = self._preds(rng, 8)
            lam = float(rng.choice([0, .25, .5, 1]))
            cfg = RankingConfig(weights=[{"endpoint": "logd", "weight": float(rng.uniform(0, 10)) + .1, "higher_is_better": bool(trial % 2)},
                                         {"endpoint": "ames", "weight": float(rng.uniform(0, 10)), "higher_is_better": False}],
                                limits=[{"endpoint": "logd", "min": 0.0, "use_unfavorable_bound": bool(trial % 3 == 0)}], lam=lam)
            res = rank_compounds(preds, cfg, info)
            for r in res:
                assert 0 <= r.property_score <= 1 + 1e-9 and 0 <= r.confidence_score <= 1 + 1e-9
                assert abs(r.blended_score - ((1 - lam) * r.property_score + lam * r.confidence_score)) < 1e-9
                assert (not r.passes_limits) == bool(r.limit_failures)
            passers = [r for r in res if r.passes_limits]
            failers = [r for r in res if not r.passes_limits]
            for f in failers:
                assert all(f.blended_rank > p.blended_rank and f.property_rank > p.property_rank for p in passers)
            assert sorted(r.blended_rank for r in passers) == sorted(r.blended_rank for r in passers)
            for grp in (passers, failers):
                for x in grp:
                    for y in grp:
                        if x.blended_score > y.blended_score:
                            assert x.blended_rank < y.blended_rank
                        if x.blended_score == y.blended_score:
                            assert x.blended_rank == y.blended_rank
            if lam == 0:
                assert all(r.blended_rank == r.property_rank for r in res)
            # permutation invariance
            keys = list(preds)
            rng.shuffle(keys)
            res2 = {r.compound_id: r for r in rank_compounds({k: preds[k] for k in keys}, cfg, info)}
            assert all(res2[r.compound_id].blended_rank == r.blended_rank for r in res)

    def test_ranking_edge_semantics(self):
        from backend.ranking.engine import RankingError, binary_entropy, confidence, desirability
        from backend.schemas.contracts import Prediction, UncertaintyBreakdown

        assert desirability(10, 0, 5, True) == 1 and desirability(-1, 0, 5, True) == 0
        assert desirability(1, 0, 4, False) == 0.75
        with pytest.raises(RankingError):
            desirability(1, 2, 2, True)
        assert binary_entropy(0) == binary_entropy(1) == 0 and abs(binary_entropy(.5) - 1) < 1e-12
        bd = UncertaintyBreakdown(ensemble_disagreement=.5, similarity_to_training=.3, assay_noise=.2)
        wide = Prediction(endpoint="logd", kind="regression", model_version_id=uuid.uuid4(), ensemble_n=5, value=0, interval_low=-50,
                          interval_high=50, coverage=.9, sample_size=1, method_note="t", breakdown=bd)
        assert confidence(wide, 1.0) == 0.0
        with pytest.raises(RankingError):
            confidence(wide, None)

    @defect("F-75", "LOW", "weights are unbounded (spec: slider 0-10): two weights of 1e308 overflow the sum and silently zero every score, so all compounds tie")
    def test_weight_bounds(self):
        from backend.schemas.contracts import RankingConfig

        with pytest.raises(Exception):  # noqa: B017
            RankingConfig(weights=[{"endpoint": "logd", "weight": 1e308, "higher_is_better": True},
                                   {"endpoint": "sol", "weight": 1e308, "higher_is_better": True}])

    def _insert_pred(self, su2, uid, cid, n, value, mv=None, endpoint="logd"):
        from api_support import MODEL_VERSION

        su2.execute(
            "insert into prediction (owner_id, compound_id, endpoint, model_version_id, ensemble_n, kind, value, interval_low, interval_high,"
            " coverage, sample_size, method_note, uncertainty_breakdown) values (%s,%s,%s,%s,%s,'regression',%s,%s,%s,.9,10,'t',"
            "'{\"ensemble_disagreement\":0.5,\"similarity_to_training\":0.3,\"assay_noise\":0.2}')",
            (uid, cid, endpoint, str(mv or MODEL_VERSION), n, value, value - 1, value + 1))

    def _rank_setup(self, mk, su2, widths):
        from api_support import FakeRegistry

        class Reg(FakeRegistry):
            def _card(self, e):
                c = super()._card(e)
                return c.model_copy(update={"benchmark": {"test_only": True, "median_calibration_width_by_n": widths}})

        app = mk(Reg())
        a = Cl(app)
        uid = a.signup("a@example.com")
        pid = a.post("/projects", {"name": "x"}).json()["id"]
        comps = a.post(f"/projects/{pid}/compounds", {"smiles": ["CCO", "CCN"]}).json()["compounds"]
        return app, a, uid, pid, comps

    def test_mixed_n_uses_each_predictions_own_width(self, mk, su2):
        app, a, uid, pid, comps = self._rank_setup(mk, su2, {"5": 4.0, "20": 1.0})
        self._insert_pred(su2, uid, comps[0]["id"], 5, 1.0)
        self._insert_pred(su2, uid, comps[1]["id"], 20, 1.0)  # interval width 2.0
        r = a.put(f"/projects/{pid}/ranking", {"weights": [{"endpoint": "logd", "weight": 1, "higher_is_better": True}]}).json()
        conf = {x["compound_id"]: x["confidence_score"] for x in r["ranking"]}
        assert abs(conf[comps[1]["id"]] - (1 - 2.0 / (2 * 1.0))) < 1e-9  # = 0.0 with N=20 width

    def test_ranking_flags_stale_model_version(self, mk, su2):
        app, a, uid, pid, comps = self._rank_setup(mk, su2, {"5": 4.0})
        old = uuid.uuid4()
        su2.execute("insert into model_version (id, endpoint, version) values (%s,'test','old')", (str(old),))
        self._insert_pred(su2, uid, comps[0]["id"], 5, 1.0, mv=old)
        self._insert_pred(su2, uid, comps[1]["id"], 5, 1.0, mv=old)
        r = a.put(f"/projects/{pid}/ranking", {"weights": [{"endpoint": "logd", "weight": 1, "higher_is_better": True}]})
        assert r.status_code in (409, 422) or "stale" in r.text

    def test_chunked_body_cap(self, mk):
        app, a, _ = self._two(mk)
        pid = a.post("/projects", {"name": "x"}).json()["id"]

        def gen():
            yield b'{"format":"smiles","smiles":["'
            for _ in range(3000):
                yield b"C" * 1000
            yield b'"]}'

        r = a.http.post(f"/projects/{pid}/compounds", content=gen(), headers={**a.h(), "content-type": "application/json"})
        assert r.status_code == 413

    def test_job_queue_memory_bounded(self):
        from backend.jobs.queue import MAX_TRACKED, InProcessJobQueue

        q, ev, u, p = InProcessJobQueue(max_workers=2), threading.Event(), uuid.uuid4(), uuid.uuid4()
        q.submit(u, p, 1, 0, lambda pr: ev.wait(10))
        q.submit(u, p, 1, 0, lambda pr: ev.wait(10))
        for _ in range(MAX_TRACKED + 200):
            q.submit(u, p, 1, 0, lambda pr: None)
        n = len(q._jobs)
        ev.set()
        assert n <= MAX_TRACKED

    def test_per_user_job_quota(self, mk):
        from backend.jobs.queue import InProcessJobQueue

        ev = threading.Event()
        q = InProcessJobQueue(max_workers=1)
        from api_support import FakeRegistry

        class Slow(FakeRegistry):
            def predict(self, *a, **k):
                ev.wait(10)
                return super().predict(*a, **k)

        app = mk(Slow(), q)
        a = Cl(app)
        a.signup("a@example.com")
        pid = a.post("/projects", {"name": "x"}).json()["id"]
        a.post(f"/projects/{pid}/compounds", {"smiles": ["CCO"]})
        codes = [a.post(f"/projects/{pid}/predict", {"endpoints": ["logd"]}).status_code for _ in range(40)]
        ev.set()
        q.shutdown()  # drain queued jobs before the fixture truncates the tables (else: deadlock)
        assert any(c in (409, 429) for c in codes)

    def test_compound_sandbox_concurrency_cap(self, monkeypatch):
        import subprocess

        from backend.compounds import sandbox

        cur = {"n": 0, "max": 0}
        lock = threading.Lock()

        def fake(argv, **kw):
            with lock:
                cur["n"] += 1
                cur["max"] = max(cur["max"], cur["n"])
            time.sleep(0.2)
            with lock:
                cur["n"] -= 1
            return subprocess.CompletedProcess(argv, 0, b'{"records": []}', b"")

        monkeypatch.setattr(sandbox.subprocess, "run", fake)
        ts = [threading.Thread(target=sandbox.run, args=({"op": "standardize"},)) for _ in range(24)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert cur["max"] <= 8

    def test_ranking_config_persisted(self, mk, su2):
        app, a, uid, pid, comps = self._rank_setup(mk, su2, {"5": 4.0})
        self._insert_pred(su2, uid, comps[0]["id"], 5, 1.0)
        self._insert_pred(su2, uid, comps[1]["id"], 5, 1.0)
        r = a.put(f"/projects/{pid}/ranking", {"weights": [{"endpoint": "logd", "weight": 1, "higher_is_better": True}]}).json()
        assert r["persisted"] is True


# ===================================================================================== C. DOCKING PREP
class TestDockprep:
    def _pdb(self):
        sys.path.insert(0, str(ROOT / "tests" / "dockprep"))
        from _fixtures import build_1m17_like

        return build_1m17_like()

    def test_change_log_matches_actual_changes_and_no_coordinate_edits(self):
        from backend.dockprep.pdbio import parse_pdb
        from backend.dockprep.protein import prepare_protein

        pdb = self._pdb()
        res = prepare_protein(pdb.encode(), add_h=False)
        inp, out = parse_pdb(pdb), parse_pdb(res["files"]["receptor.pdb"])
        key = lambda a: (a.chain, a.resseq, a.icode, a.resname)  # noqa: E731
        removed = {key(a) for a in inp.atoms} - {key(a) for a in out.atoms}
        logged = {t for e in res["change_log"] for t in [e["target"]] if e["step"] in ("waters", "hetero")}
        for c, s, ic, n in removed:
            assert f"{c}:{s}{ic}" in logged or any(f"{c}:{s}{ic}" in t for t in logged), (c, s, n)
        ref = {(a.chain, a.resseq, a.icode, a.name): (a.x, a.y, a.z) for a in inp.atoms if not a.altloc or a.altloc == "A"}
        for a in out.atoms:
            k = (a.chain, a.resseq, a.icode, a.name)
            if k in ref:
                assert max(abs(p - q) for p, q in zip(ref[k], (a.x, a.y, a.z))) < 1e-3
        assert any(e["step"] == "altloc" for e in res["change_log"])

    def test_ligand_and_residue_box_math_on_1m17(self):
        import numpy as np

        from backend.dockprep.pdbio import parse_pdb
        from backend.dockprep.sites import analyze_sites

        pdb = self._pdb()
        sites = {s["source"]: s for s in analyze_sites(pdb.encode(), ["ligand", "residues"], ["A:694", "A:721"], None, None, 4.0, None)["sites"]}
        lig = np.array([[a.x, a.y, a.z] for a in parse_pdb(pdb).atoms if a.resname == "AQ4"])
        lo, hi = lig.min(0), lig.max(0)
        assert np.allclose(sites["cocrystal_ligand"]["center"], (lo + hi) / 2, atol=2e-3)
        assert np.allclose(sites["cocrystal_ligand"]["size"], hi - lo + 8, atol=2e-3)
        res = np.array([[a.x, a.y, a.z] for a in parse_pdb(pdb).atoms if a.resseq in (694, 721) and a.record == "ATOM"])
        assert np.allclose(sites["user_residues"]["size"], res.max(0) - res.min(0) + 8, atol=2e-3)

    def test_no_verdict_language_in_outputs(self):
        from backend.dockprep.protein import prepare_protein
        from backend.dockprep.sites import analyze_sites

        pdb = self._pdb()
        txt = json.dumps(prepare_protein(pdb.encode(), add_h=False)) + json.dumps(
            analyze_sites(pdb.encode(), ["ligand", "residues", "detect"], ["A:694"], None, None, 4.0, None))
        assert not re.search(r"\b(good|bad|best|excellent|druggab\w*|recommend\w*|promising|poor|optimal|suitable|safe|toxic)\b", txt, re.I)

    def test_bundle_zip_names_are_flat(self):
        import io
        import zipfile

        from backend.dockprep.bundle import make_zip

        z = zipfile.ZipFile(io.BytesIO(make_zip({"receptor.pdbqt": "x", "cocrystal_AQ4_A999.pdb": "y"}, [], {})))
        assert all(not n.startswith("/") and ".." not in n and "\\" not in n for n in z.namelist())

    def test_hostile_names_and_ids(self):
        from backend.dockprep.errors import DockprepError
        from backend.dockprep.fetch import RcsbFetcher, validate_pdb_id
        from backend.dockprep.limits import sanitize_name

        for bad in ("../../etc/passwd", "1M17/../x", "1M17\n2", "http://evil/1M1", "12345", ""):
            with pytest.raises(DockprepError):
                validate_pdb_id(bad)
        calls = []
        f = RcsbFetcher(lambda url, mb, t: calls.append(url) or b"x")
        f.fetch_pdb("1m17")
        assert calls == ["https://files.rcsb.org/download/1M17.pdb"]
        assert "/" not in sanitize_name("..\\..\\a/b") and not sanitize_name(".hidden").startswith(".")

    def test_urllib_get_refuses_foreign_hosts_and_http(self):
        from backend.dockprep.errors import DockprepError
        from backend.dockprep.fetch import urllib_http_get

        for u in ("http://files.rcsb.org/x", "https://evil.example/x", "https://files.rcsb.org.evil.example/x", "https://127.0.0.1/x"):
            with pytest.raises(DockprepError):
                urllib_http_get(u, 10, 1)

    def test_fetch_disabled_by_default(self, monkeypatch):
        from backend.dockprep.errors import DockprepError
        from backend.dockprep.router import get_fetcher

        monkeypatch.delenv("METIS_DOCKPREP_FETCH", raising=False)
        with pytest.raises(DockprepError):
            get_fetcher()

    def test_busy_slots_return_503_not_queue(self):
        from backend.dockprep import sandbox
        from backend.dockprep.errors import DockprepError

        for _ in range(sandbox.MAX_CONCURRENT_JOBS):
            assert sandbox._slots.acquire(blocking=False)
        try:
            with pytest.raises(DockprepError) as ei:
                sandbox.run_sandboxed("ligand", {"smiles": "CCO"})
            assert ei.value.status == 503
        finally:
            for _ in range(sandbox.MAX_CONCURRENT_JOBS):
                sandbox._slots.release()

    def test_sandbox_child_env_is_minimal(self, monkeypatch):
        import subprocess

        from backend.dockprep import sandbox

        monkeypatch.setenv("METIS_SECRET_KEY", "leak-me-please-0123456789abcdef")
        monkeypatch.setenv("METIS_DATABASE_URL", "postgresql://x:y@h/db")
        seen = {}
        real = subprocess.run
        monkeypatch.setattr(sandbox.subprocess, "run", lambda a, **k: seen.update(k) or real(a, **k))
        sandbox.run_sandboxed("ligand", {"smiles": "CCO", "n_conformers": 1})
        assert not any(k.startswith("METIS_S") or k.startswith("METIS_DATABASE") for k in seen["env"])
        assert seen["cwd"] != str(ROOT)

    def test_protein_prep_scales(self):
        import random

        from backend.dockprep.protein import prepare_protein

        random.seed(1)
        lines = ["ATOM  %5d  CA  ALA A%4d    %8.3f%8.3f%8.3f  1.00 20.00           C" % (i % 100000, i % 10000, random.uniform(0, 150),
                                                                                        random.uniform(0, 150), random.uniform(0, 150)) for i in range(1, 8001)]
        t = time.time()
        prepare_protein(("\n".join(lines) + "\nEND\n").encode(), add_h=False)
        assert time.time() - t < 3.0

    def test_detect_on_spread_structure_is_bounded(self):
        import itertools

        from backend.dockprep import sandbox
        from backend.dockprep.errors import DockprepError

        lines, n = [], 0
        for x, y, z in itertools.product((0, 300), repeat=3):
            for k in range(4):
                n += 1
                lines.append("ATOM  %5d  CA  ALA A%4d    %8.3f%8.3f%8.3f  1.00 20.00           C" % (n, n, x + k, y, z))
        payload = {"data_b64": base64.b64encode(("\n".join(lines) + "\nEND\n").encode()).decode(), "modes": ["detect"]}
        try:
            sandbox.run_sandboxed("sites", payload, timeout=12)
        except DockprepError as e:
            assert e.code != "timeout"

    def test_altloc_policy_matches_log(self):
        from backend.dockprep.pdbio import parse_pdb
        from backend.dockprep.protein import ChangeLog, resolve_altlocs

        def ln(i, name, alt, occ, x):
            return "ATOM  %5d %-4s%1s%3s A%4d    %8.3f%8.3f%8.3f%6.2f 20.00           %s" % (i, name, alt, "LYS", 5, x, 0, 0, occ, name[0])

        st = parse_pdb("\n".join([ln(1, "CD", "A", .4, 1), ln(2, "CE", "A", .4, 2), ln(3, "NZ", "A", .4, 3), ln(4, "CD", "B", .6, 1.1)]) + "\nEND\n")
        log = ChangeLog()
        resolve_altlocs(st.atoms, log)
        means = log.entries[0]["params"]["mean_occupancy_by_altloc"]
        assert log.entries[0]["params"]["kept"] == max(means, key=means.get)


# ============================================================================================= D. ML
@pytest.fixture(scope="module")
def reg(tmp_path_factory):
    import w2ml

    root = tmp_path_factory.mktemp("w2reg")
    (root / "m.yaml").write_text(w2ml.MANIFEST)
    out = w2ml.build_registry(root, root / "m.yaml")
    return root, out, w2ml


def _clone(reg, tmp_path):
    root, out, w2ml = reg
    d = tmp_path / "r"
    shutil.copytree(root, d)
    return d, root / "m.yaml", w2ml


class TestML:
    def test_registry_roundtrip_is_consistent(self, reg):
        from ml.registry.registry import FileModelRegistry

        r = FileModelRegistry(reg[0])
        d1, d2 = r.predict("synreg", "CCOc1ccccc1", 5), r.predict("synreg", "CCOc1ccccc1", 5)
        assert d1.prediction == d2.prediction and d1.fragments == d2.fragments  # deterministic (decomposition + attribution)
        p = d1.prediction
        assert p.interval_low <= p.value <= p.interval_high
        b = p.breakdown
        assert abs(b.ensemble_disagreement + b.similarity_to_training + b.assay_noise - 1) < 1e-6
        assert "Assay noise is not estimated" in p.method_note
        c = r.predict("syncls", "CCOc1ccccc1", 10).prediction
        assert 0 <= c.probability <= 1 and c.kind == "classification"

    def test_tampered_member_or_manifest_hash_refused(self, reg, tmp_path):
        from ml.registry.registry import FileModelRegistry

        d, _, _ = _clone(reg, tmp_path)
        m = d / "artifacts" / "synreg" / "v1" / "members" / "member_03.ckpt"
        m.write_bytes(m.read_bytes() + b"x")
        with pytest.raises(ValueError):
            FileModelRegistry(d).predict("synreg", "CCO", 5)

    def test_license_gate_direct_calls_and_aliases(self, tmp_path):
        from ml.data.manifest import LicenseError, assert_trainable, load_manifest
        from ml.train import train_endpoint as TE

        with pytest.raises(LicenseError):
            assert_trainable("FreeSolv")
        base = ('datasets:\n - id: {id}\n   name: "{name}"\n   source_url: {url}\n   endpoint: e\n   kind: regression\n   units: u\n'
                '   license_as_stated: "{lic}"\n   license_status: cleared\n   date_checked: "2026-01-02"\n   confirmed_by: "Owner"\n   notes: n\n')
        for name, url, lic in (("FreeSolv", "https://x.org/a", "CC BY 4.0"), ("hydration", "https://github.com/MobleyLab/FreeSolv", "CC BY 4.0"),
                               ("Free-Solv", "https://x.org/a", "CC0"), ("ok", "https://x.org", "CC BY-NC-SA 4.0"), ("ok", "https://x.org", "CC BY-ND 4.0"),
                               ("ok", "https://x.org", "Not Specified")):
            p = tmp_path / "m.yaml"
            p.write_text(base.format(id="d1", name=name, url=url, lic=lic))
            with pytest.raises(LicenseError):
                load_manifest(p)
        pend = tmp_path / "p.yaml"
        pend.write_text(base.format(id="d1", name="n", url="https://x.org", lic="x").replace("cleared", "pending"))
        out = tmp_path / "arts"
        with pytest.raises(LicenseError):
            TE.run_training({"endpoint": "e", "version": "v", "datasets": ["d1"], "kind": "regression", "backend": "sklearn"},
                            manifest_path=pend, loader=lambda c: pytest.fail("data read before gate"), artifacts_root=out)
        assert not out.exists()

    @pytest.mark.parametrize("lic", ["CC BY 4.0 NoCommercial", "CC BY 4.0 (not for commercial use)", "CC BY-ＮＣ 4.0"])
    def test_licence_heuristics(self, tmp_path, lic):
        from ml.data.manifest import LicenseError, load_manifest

        p = tmp_path / "m.yaml"
        p.write_text('datasets:\n - id: d1\n   name: n\n   source_url: https://x.org\n   endpoint: e\n   kind: regression\n   units: u\n'
                     f'   license_as_stated: "{lic}"\n   license_status: cleared\n   date_checked: "2026-01-02"\n   confirmed_by: "Owner"\n   notes: n\n')
        with pytest.raises(LicenseError):
            load_manifest(p)

    def test_publish_rejects_mismatched_benchmark(self, reg, tmp_path):
        from ml.registry.publish import PublishRefused, publish

        d, man, w2ml = _clone(reg, tmp_path)
        mols, yreg = reg[1]["mols"], reg[1]["yreg"]
        rep = w2ml.bench("regression", mols, yreg, "synreg", frac=(0.6, 0.2, 0.2))
        with pytest.raises(PublishRefused):
            publish(d / "artifacts" / "synreg" / "v1", rep, registry_root=d, manifest_path=man)

    def test_calibration_tamper_detected(self, reg, tmp_path):
        from ml.registry.registry import FileModelRegistry

        d, _, _ = _clone(reg, tmp_path)
        f = d / "artifacts" / "synreg" / "v1" / "calibration.json"
        cal = json.loads(f.read_text())
        for n in cal["conformal"]:
            cal["conformal"][n]["q"] *= 0.01
        f.write_text(json.dumps(cal))
        with pytest.raises(Exception):  # noqa: B017
            FileModelRegistry(d).predict("synreg", "CCO", 5)

    def test_load_bundle_requires_hash(self, reg, tmp_path):
        import joblib

        from ml.train.bundle import load_bundle

        d, _, _ = _clone(reg, tmp_path)
        marker = tmp_path / "pwned"

        class E:
            def __reduce__(self):
                return (pathlib.Path(str(marker)).write_text, ("x",))

        b = d / "artifacts" / "synreg" / "v1"
        joblib.dump(E(), b / "members" / "member_00.ckpt")
        try:
            load_bundle(b)
        except Exception:  # noqa: BLE001, S110
            pass
        assert not marker.exists()

    def test_publish_dataset_provenance(self, reg, tmp_path):
        from ml.registry.publish import PublishRefused, publish

        d, man, w2ml = _clone(reg, tmp_path)
        b = d / "artifacts" / "synreg" / "v1"
        meta = json.loads((b / "meta.json").read_text())
        meta["datasets"][0]["id"] = "SynthCls"
        (b / "meta.json").write_text(json.dumps(meta))
        (d / "manifest.json").unlink()
        with pytest.raises((PublishRefused, ValueError)):
            publish(b, reg[1]["synreg"][1], registry_root=d, manifest_path=man)

    def test_published_card_feeds_ranking(self, reg):
        from backend.ranking.service import median_width
        from ml.registry.registry import FileModelRegistry

        card = FileModelRegistry(reg[0]).model_card("synreg")
        w = median_width(card, 5)
        assert w is not None and w > 0

    def test_conformal_quantile_rank_and_coverage(self):
        import math

        import numpy as np

        from ml.uncertainty.conformal import SplitConformal, _conformal_q

        for n in range(1, 2000):
            assert math.ceil((n + 1) * 0.9) == -((-(n + 1) * 9) // 10)
        s = np.arange(1.0, 10.0)
        assert _conformal_q(s, 0.1) == 9.0 and _conformal_q(s[:-1], 0.1) == float("inf")
        rng = np.random.default_rng(0)
        cov = []
        for _ in range(100):
            sim = rng.uniform(.2, .9, 560)
            y = rng.normal(0, 1.5 - sim)
            c = SplitConformal.fit(np.zeros(60), y[:60], sim[:60])
            lo, hi = c.interval(np.zeros(500), sim[60:])
            cov.append(np.mean((y[60:] >= lo) & (y[60:] <= hi)))
        assert 0.88 <= float(np.mean(cov)) <= 0.95

    def test_venn_abers_and_isotonic_edges(self):
        import numpy as np

        from ml.uncertainty.conformal import IsotonicRecalibrator, VennAbers

        rng = np.random.default_rng(1)
        for labels in ([0] * 6, [1] * 6, [0, 1, 0, 1, 1, 1], list(rng.integers(0, 2, 6))):
            va = VennAbers().fit([.5, .5, .1, .9, .5, .3], labels)
            p0, p1 = va.predict_interval([0.0, .5, 1.0])
            p = va.predict([0.0, .5, 1.0])
            assert np.all(p0 <= p1) and np.all((0 <= p) & (p <= 1)) and np.all(np.isfinite(p))
            iso = IsotonicRecalibrator().fit([.5, .5, .1, .9, .5, .3], labels).predict([-1, 2])
            assert np.all((0 <= iso) & (iso <= 1))

    def test_decompose_shares_valid(self):
        import numpy as np

        from ml.uncertainty.decompose import decompose

        rng = np.random.default_rng(0)
        for _ in range(50):
            d = decompose(rng.normal(0, rng.uniform(0, 3), int(rng.choice([3, 5, 10, 20]))), float(rng.uniform(0, 3)), None, seed=1)
            b = d.to_breakdown()
            assert abs(b.ensemble_disagreement + b.similarity_to_training + b.assay_noise - 1) < 1e-3 and b.assay_noise == 0
            assert all(0 <= lo <= hi <= 1 for lo, hi in d.ranges.values())
        with pytest.raises(Exception):  # noqa: B017
            decompose(np.array([1.0, np.nan, 2.0]), 1.0).to_breakdown()

    def test_attribution_partition_determinism_and_index_map(self):
        import numpy as np
        from rdkit import Chem

        from backend.compounds.standardize import embed_3d
        from ml.uncertainty.attribution import attribute

        fn = lambda ss: np.array([[len(s) * 0.1 + (s.count("O") * .3) for s in ss]] * 3) + np.arange(3)[:, None] * .01  # noqa: E731
        for smi in ("CCOc1ccccc1C(=O)O", "CC(=O)Nc1ccc(O)cc1", "c1ccc2[nH]ccc2c1", "CCO"):
            a, b = attribute(smi, fn, seed=3), attribute(smi, fn, seed=3)
            assert a.fragments == b.fragments
            atoms = sorted(i for f in a.fragments for i in f.atom_indices)
            assert atoms == list(range(Chem.MolFromSmiles(smi).GetNumAtoms()))
            assert abs(sum(f.share for f in a.fragments) - 1) < 1e-9
            mb = Chem.MolFromMolBlock(embed_3d(smi)["molblock"])
            assert [x.GetSymbol() for x in mb.GetAtoms()] == [x.GetSymbol() for x in Chem.MolFromSmiles(smi).GetAtoms()]

    def test_splits_no_scaffold_or_smiles_overlap(self, reg):
        from rdkit import Chem
        from rdkit.Chem.Scaffolds import MurckoScaffold

        from ml.data.splits import scaffold_split

        mols = reg[1]["mols"]
        parts = scaffold_split(mols, frac=(0.7, 0.15, 0.15), seed=1)
        sc = [{MurckoScaffold.MurckoScaffoldSmiles(mol=Chem.MolFromSmiles(mols[i])) for i in p if MurckoScaffold.MurckoScaffoldSmiles(mol=Chem.MolFromSmiles(mols[i]))} for p in parts]
        assert not (sc[0] & sc[1]) and not (sc[0] & sc[2]) and not (sc[1] & sc[2])
        assert sorted(i for p in parts for i in p) == list(range(len(mols)))

    def test_ml_has_no_network_code(self):
        src = "\n".join(p.read_text() for d in ("train", "uncertainty", "benchmarks", "registry") for p in (ROOT / "ml" / d).rglob("*.py"))
        assert not re.search(r"^\s*(import|from)\s+(requests|urllib|httpx|socket|http\.client|boto3|tdc|ftplib)\b", src, re.M)

    def test_time_estimate_formula_is_linear_in_compounds(self, reg):
        from ml.registry.registry import FileModelRegistry

        r = FileModelRegistry(reg[0])
        t1, t2, t3 = (r.time_estimate_seconds("synreg", 5, n) for n in (1, 2, 3))
        assert abs((t3 - t2) - (t2 - t1)) < 1e-9 and t1 > 0
        with pytest.raises(ValueError):
            r.time_estimate_seconds("synreg", 7, 1)

    @defect("F-105", "MEDIUM", "silently-wrong label: for classification the Prediction.coverage field carries 1 - ECE (not a coverage), and regression coverage is the NOMINAL 0.9, not the measured test coverage; exports print both as 'coverage'")
    def test_classification_coverage_is_not_a_calibration_score(self, reg):
        from ml.registry.registry import FileModelRegistry

        r = FileModelRegistry(reg[0])
        card = r.model_card("syncls")
        p = r.predict("syncls", "CCOc1ccccc1", 5).prediction
        assert p.coverage != card.coverage_by_n["5"] or "ECE" not in p.method_note


# ============================================================================== E. DEPLOY / CI CONSISTENCY
class TestDeploy:
    def test_runtime_requirements_pinned(self):
        for f in ("requirements.txt", "ml/requirements.txt"):
            for ln in (ROOT / f).read_text().splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and not ln.startswith("numpy>="):  # numpy range: F-111
                    assert "==" in ln, (f, ln)

    def test_image_contains_disclaimer(self):
        assert "docs/legal" in (ROOT / "deploy" / "Dockerfile").read_text()

    def test_image_contains_ml(self):
        d = (ROOT / "deploy" / "Dockerfile").read_text()
        assert re.search(r"COPY[^\n]*\bml/", d) and "ml/requirements.txt" in d

    def test_model_version_rows_are_created_somewhere(self):
        src = "".join(p.read_text() for d in ("backend", "ml", "scripts") for p in (ROOT / d).rglob("*.py"))
        assert re.search(r"INSERT\s+INTO\s+model_version", src, re.I)

    def test_ci_lint_scope_covers_new_packages(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        line = next(ln for ln in ci.splitlines() if "ruff check" in ln)
        assert "backend/accounts" in line or "backend ml" in line or re.search(r"ruff check( |$)[^\n]*\bbackend\b(?!/core)", line)

    def test_makefile_and_ci_sast_scope_match(self):
        mk = (ROOT / "Makefile").read_text()
        assert "-r backend ml" in mk and not re.search(r"numpy>=", (ROOT / "requirements.txt").read_text())

    def test_main_mounts_all_new_routers(self):
        from backend.main import create_app

        paths = set(create_app().openapi()["paths"])
        assert {"/projects", "/dockprep/proteins", "/auth/login", "/jobs/{job_id}", "/models"} <= paths
