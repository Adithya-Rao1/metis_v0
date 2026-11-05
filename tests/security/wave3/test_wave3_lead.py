"""Metis Lead Studio (Beta) behind the REAL Metis session (no dependency overrides): anonymous = 401 on every /lead route,
CSRF and the terms gate on every mutation, cross-user 404s, the retired studio cookie grants nothing, logout kills streams."""
from __future__ import annotations

import json
import re
import uuid

import pytest
import w2stack as S
from w2stack import Cl

from leadstudio.server.core import Settings
from leadstudio.server.state import build_state

NITRO = "CCOC(=O)c1ccc(cc1)[N+](=O)[O-]"
ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"


@pytest.fixture
def lead(mk3):
    app = mk3()
    app.state.lead = build_state(settings=Settings(force_mock=True, mock_delay=0.0, replay_delay=0.0, runs_per_minute=1000, per_user_active=10))
    return app


def lead_ops(app):
    out = []
    for path0, ops in app.openapi()["paths"].items():
        if path0.startswith("/lead"):
            for m in sorted({k.upper() for k in ops} - {"HEAD", "OPTIONS"}):
                path = path0
                for p in set(re.findall(r"\{[^}]+\}", path0)):
                    path = path.replace(p, str(uuid.uuid4()))
                out.append((m, path0, path))
    return out


BODY = {"POST": {}, "PUT": {}, "DELETE": None, "GET": None}


def test_the_sweep_sees_every_lead_route(lead):
    ops = lead_ops(lead)
    assert len(ops) >= 30
    assert {p.split("/")[2] for _, p, _ in ops} == {"api", "library", "profiles"}


def test_anonymous_is_401_on_every_lead_route(lead):
    anon = Cl(lead)
    for m, p0, url in lead_ops(lead):
        r = anon.req(m, url, json=BODY[m])          # with a valid anonymous CSRF pair, so the 401 is authentication, not CSRF
        assert r.status_code == 401, (m, p0, r.status_code)
        assert "detail" in r.json() and "Traceback" not in r.text


def test_anonymous_without_csrf_never_reaches_a_handler(lead):
    anon = Cl(lead)
    for m, p0, url in lead_ops(lead):
        r = anon.http.request(m, url, json=BODY[m])
        assert r.status_code in (401, 403), (m, p0, r.status_code)


def test_signed_in_but_no_csrf_header_is_403_on_every_mutation(lead):
    c = Cl(lead)
    c.signup("a@example.org")
    n = 0
    for m, p0, url in lead_ops(lead):
        if m == "GET":
            continue
        r = c.http.request(m, url, json=BODY[m])    # session cookie present, CSRF header deliberately absent
        assert r.status_code == 403 and r.json()["detail"] == "csrf_failed", (m, p0, r.status_code)
        n += 1
    assert n >= 15
    bad = c.http.request("POST", "/lead/api/runs", json={"seed_smiles": NITRO}, headers={S.CSRF_HEADER: "forged"})
    assert bad.status_code == 403


def test_signed_in_gets_through_and_streams(lead):
    c = Cl(lead)
    c.signup("a@example.org")
    h = c.get("/lead/api/health")
    assert h.status_code == 200 and h.json()["source"] == "mock"
    r = c.post("/lead/api/runs", {"seed_smiles": NITRO})
    assert r.status_code == 200 and r.json()["source"] == "mock"
    s = c.get(f"/lead/api/runs/{r.json()['run_id']}/events")
    assert s.status_code == 200 and "event: run_end" in s.text


def test_terms_gate_blocks_mutations_until_reaccepted(mk3):
    app = mk3()
    app.state.lead = build_state(settings=Settings(force_mock=True, mock_delay=0.0, replay_delay=0.0))
    c = Cl(app)
    c.signup("a@example.org")
    app.state.accounts_config = app.state.accounts_config.model_copy(update={"terms_version": "2099-01"})
    assert c.get("/lead/api/health").status_code == 200            # reads are not gated
    for url, body in (("/lead/api/runs", {"seed_smiles": NITRO}), ("/lead/profiles", {"name": "x"}),
                      ("/lead/library/manual", {"steps": []})):
        r = c.post(url, body)
        assert r.status_code == 403 and r.json()["detail"] == "terms_reacceptance_required", url


def test_cross_user_everything_is_404_with_real_sessions(lead):
    a, b = Cl(lead), Cl(lead)
    a.signup("a@example.org")
    b.signup("b@example.org")
    rid = a.post("/lead/api/runs", {"seed_smiles": NITRO}).json()["run_id"]
    evs = [json.loads(ln[6:]) for ln in a.get(f"/lead/api/runs/{rid}/events").text.splitlines() if ln.startswith("data: ")]
    lib = a.post("/lead/library/runs", {"events": evs}).json()["id"]
    man = a.post("/lead/library/manual", {"steps": [{"parent_smiles": NITRO, "child_smiles": ASPIRIN, "edit_id": "x", "site": [0]}]}).json()["id"]
    prof = a.post("/lead/profiles", {"name": "p", "objectives": {}, "avoid_smarts": [], "edit_allowlist": []}).json()["id"]
    sess = a.post("/lead/api/manual/sessions", {"seed_smiles": NITRO}).json()["session_id"]
    for m, u in (("GET", f"/lead/api/runs/{rid}/events"), ("DELETE", f"/lead/api/runs/{rid}"), ("GET", f"/lead/library/runs/{lib}"),
                 ("DELETE", f"/lead/library/runs/{lib}"), ("GET", f"/lead/library/manual/{man}"), ("DELETE", f"/lead/library/manual/{man}"),
                 ("GET", f"/lead/profiles/{prof}"), ("DELETE", f"/lead/profiles/{prof}"), ("GET", f"/lead/api/manual/sessions/{sess}/export")):
        assert b.req(m, u).status_code == 404, (m, u)
    assert b.put(f"/lead/profiles/{prof}", {"name": "x", "objectives": {}, "avoid_smarts": [], "edit_allowlist": []}).status_code == 404
    assert b.post(f"/lead/api/manual/sessions/{sess}/steps", {"parent_smiles": NITRO, "child_smiles": ASPIRIN, "edit_id": "x", "site": [0]}).status_code == 404
    assert b.get("/lead/library/runs").json() == {"runs": []} and b.get("/lead/profiles").json() == {"profiles": []}
    assert a.get(f"/lead/library/runs/{lib}").status_code == 200 and a.get(f"/lead/profiles/{prof}").status_code == 200


def test_the_old_studio_cookie_authenticates_nobody(lead):
    anon = Cl(lead)
    anon.http.cookies.set("studio_sid", "x" * 22)
    anon.http.cookies.set("studio_token", "x" * 40)
    assert anon.get("/lead/api/health").status_code == 401
    assert "studio_sid" not in {k for r in anon.req("GET", "/lead/api/health").headers.get_list("set-cookie") for k in re.findall(r"studio_sid", r)}
    assert anon.get("/lead/api/health", headers={"Authorization": "Bearer abc", "X-Studio-Key": "k"}).status_code == 401


def test_studio_auth_routes_are_not_mounted(lead):
    paths = set(lead.openapi()["paths"])
    assert not {p for p in paths if p.startswith("/lead") and ("/auth" in p or p.endswith("/me"))}
    anon = Cl(lead)
    for url in ("/lead/api/auth/login", "/lead/api/auth/signup", "/lead/api/me", "/api/me", "/me", "/lead/auth/login"):
        assert anon.req("POST", url, json={}).status_code in (401, 404, 405), url


def test_logout_ends_access_including_open_streams_endpoint(lead):
    c = Cl(lead)
    c.signup("a@example.org")
    rid = c.post("/lead/api/runs", {"seed_smiles": NITRO}).json()["run_id"]
    assert c.get(f"/lead/api/runs/{rid}/events").status_code == 200
    assert c.post("/auth/logout").status_code == 200
    assert c.get(f"/lead/api/runs/{rid}/events").status_code == 401
    assert c.get("/lead/library/runs").status_code == 401


def test_rate_limit_is_enforced_by_the_database_limiter_across_requests(mk3):
    app = mk3()
    app.state.lead = build_state(settings=Settings(force_mock=True, mock_delay=0.0, replay_delay=0.0, validates_per_minute=3))
    c = Cl(app)
    c.signup("a@example.org")
    codes = [c.post("/lead/api/molecules/validate", {"smiles": "CCO"}).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    r = c.post("/lead/api/molecules/validate", {"smiles": "CCO"})
    assert r.headers["retry-after"].isdigit()
    other = Cl(app)
    other.signup("b@example.org")
    assert other.post("/lead/api/molecules/validate", {"smiles": "CCO"}).status_code == 200


def test_error_bodies_do_not_leak_internals(lead):
    c = Cl(lead)
    c.signup("a@example.org")
    for url, body in (("/lead/api/runs", {"seed_smiles": "((("}), ("/lead/api/molecules/validate", {"smiles": "\x00"}),
                      ("/lead/api/manual/preview", {"smiles": NITRO, "edit_id": "nope", "site": [0]})):
        r = c.post(url, body)
        assert r.status_code in (200, 422) and "Traceback" not in r.text and "/Users/" not in r.text and "leadstudio" not in r.text
