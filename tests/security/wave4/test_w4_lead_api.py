"""Wave 4: Lead Studio behind the REAL Metis session. Authentication on every route and every path variant, the structure-file route
(traversal, symlinks, encodings), SSE slot accounting, input handling that must answer 4xx and never 500, per-user caps under
concurrency, cross-user isolation of poses and adapt jobs, log hygiene, and what account deletion leaves behind in process memory.

Convention: `xfail(strict=True, reason="F-xxx SEV: ...")` = reproducible defect (a hard failure once fixed: remove the marker then);
unmarked = a protection that holds."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import w2stack as S
from conftest import ASPIRIN, NITRO, cookie_header, mock_state, raw_asgi, signed_in, tolerant
from fastapi.testclient import TestClient

from leadstudio.server import stages

EXPECTED_OPS = {
    "DELETE /lead/api/runs/{run_id}", "DELETE /lead/library/manual/{sid}", "DELETE /lead/library/runs/{run_id}", "DELETE /lead/profiles/{pid}",
    "GET /lead/api/adapt/jobs/{job_id}", "GET /lead/api/data/{path}", "GET /lead/api/health", "GET /lead/api/manual/applicable",
    "GET /lead/api/manual/sessions/{session_id}/export", "GET /lead/api/poses/{job_id}", "GET /lead/api/presets",
    "GET /lead/api/runs/{run_id}/events", "GET /lead/api/targets", "GET /lead/api/targets/{target_id}", "GET /lead/library/manual",
    "GET /lead/library/manual/{sid}", "GET /lead/library/runs", "GET /lead/library/runs/{run_id}", "GET /lead/profiles",
    "GET /lead/profiles/{pid}", "POST /lead/api/adapt/jobs", "POST /lead/api/manual/preview", "POST /lead/api/manual/sessions",
    "POST /lead/api/manual/sessions/{session_id}/steps", "POST /lead/api/molecules/validate", "POST /lead/api/poses", "POST /lead/api/runs",
    "POST /lead/library/manual", "POST /lead/library/runs", "POST /lead/profiles", "POST /lead/profiles/check-smarts",
    "PUT /lead/profiles/{pid}",
}


def ops(app):
    out = []
    for path0, item in app.openapi()["paths"].items():
        if path0.startswith("/lead"):
            for m in item:
                path = path0
                for p in set(re.findall(r"\{[^}]+\}", path0)):
                    path = path.replace(p, "structures/egfr.pdb" if p == "{path}" else str(uuid.uuid4()))
                out.append((m.upper(), path0, path))
    return out


# ============================================================================================== authentication
def test_the_lead_route_inventory_is_pinned_so_a_new_route_forces_a_review(lead):
    """If this fails a route was added or removed: review its authentication, CSRF, rate limit and ownership, then update the set."""
    assert {f"{m} {p0}" for m, p0, _ in ops(lead)} == EXPECTED_OPS


def test_anonymous_gets_nothing_on_any_path_variant_of_any_route(lead):
    """Trailing slash, doubled slash, case, dot segments, HEAD/OPTIONS/TRACE: the session check sits in front of every variant."""
    seen = 0
    for m, p0, url in ops(lead):
        variants = [url, url + "/", "/" + url, url.replace("/lead/", "/LEAD/"), url.replace("/lead/", "/lead/./"), url.replace("/lead/", "/x/../lead/")]
        for v in variants:
            for method in {m, "HEAD", "OPTIONS", "TRACE", "PATCH"}:
                status, body, exc = raw_asgi(lead, method, v, [])
                assert exc is None
                assert status in (307, 308, 401, 403, 404, 405), (method, v, status)
                assert b"SOURCE" not in body and b"ATOM" not in body and b"retry:" not in body, (method, v)
                seen += 1
    assert seen > 900


def test_a_session_cookie_for_a_logged_out_user_opens_nothing(lead):
    c = signed_in(lead)
    old = dict(c.http.cookies)
    assert c.post("/auth/logout").status_code == 200
    replay = [(b"cookie", "; ".join(f"{k}={v}" for k, v in old.items()).encode())]
    for m, _p0, url in ops(lead):
        if m == "GET":
            status, _b, _e = raw_asgi(lead, m, url, replay)
            assert status == 401, (url, status)


# ============================================================================================ structure-file route
LEGIT = "/lead/api/data/structures/egfr.pdb"
TRAVERSAL = [
    "../../../../../etc/passwd", "structures/../../../../../etc/passwd", "structures/../../../backend/main.py", "structures/../targets/egfr.json",
    "structures/../../loader.py", "structures/./../loader.py", "structures//egfr.pdb/", "structures/egfr.pdb/", "structures/egfr.pdb\x00.json",
    "structures/egfr.pdb\x00", "structures\\..\\loader.py", "structures/..\\loader.py", "/etc/passwd", "//etc/passwd", "structures/.hidden.pdb",
    "structures/.git/config", ".git/config", "structures/%2e%2e/loader.py", "structures/．．/loader.py", "structures/..%2floader.py",
    "structures/egfr.py", "structures/egfr.pdb.py", "targets/egfr.json", "structures/", "structures", "", ".", "..", "structures/..",
    "structures/egfr.pdb/../../loader.py", "structures/‮/egfr.pdb", "structures/egfr.pdb:stream", "structures/egfr.pdb%", "structures/ egfr.pdb",
    "structures/egfr.pdb ", "structures/" + "a" * 190 + ".pdb", "structures/" + "../" * 60 + "etc/passwd",
]


def test_the_structure_route_serves_the_bundled_structure_and_only_that_kind_of_file(lead):
    c = signed_in(lead)
    r = c.get(LEGIT)
    assert r.status_code == 200 and r.text.startswith("REMARK") and r.headers["content-type"].startswith("text/plain")
    assert c.get("/lead/api/data/egfr.pdb").status_code == 200       # the documented short form (the loader's structure_url has the prefix)
    assert c.get("/lead/api/data/structures/egfr.pdb", headers={"Range": "bytes=0-9"}).status_code in (200, 206)


@pytest.mark.parametrize("rel", TRAVERSAL)
def test_traversal_and_encoding_tricks_never_leave_the_structures_directory(lead, rel):
    c = signed_in(lead)
    status, body, exc = raw_asgi(lead, "GET", "/lead/api/data/" + rel, [cookie_header(c)])
    assert exc is None and status in (404, 422, 307, 308), (rel, status, body[:80])
    assert b"root:" not in body and b"import " not in body and b'"id"' not in body and b"ATOM" not in body


def test_resolve_data_path_refuses_symlinks_directories_and_hidden_files(tmp_path, monkeypatch):
    root, outside = tmp_path / "structs", tmp_path / "outside"
    (root / "sub").mkdir(parents=True)
    outside.mkdir()
    (root / "ok.pdb").write_text("ATOM")
    (root / "sub" / "ok2.sdf").write_text("x")
    (root / "UP.PDB").write_text("ATOM")
    (outside / "secret.pdb").write_text("SECRET")
    (outside / "secret.json").write_text("{}")
    (root / "link_out.pdb").symlink_to(outside / "secret.pdb")
    (root / "linkdir").symlink_to(outside, target_is_directory=True)
    (root / "link_inside.pdb").symlink_to(root / "ok.pdb")
    (root / ".hid.pdb").write_text("x")
    (root / "dir.pdb").mkdir()
    (root / "note.txt").write_text("x")
    (root / "meta.json").write_text("{}")
    monkeypatch.setenv("STUDIO_STRUCTURES_DIR", str(root))
    ok = lambda rel: stages.resolve_data_path(rel) is not None  # noqa: E731
    assert ok("ok.pdb") and ok("structures/ok.pdb") and ok("sub/ok2.sdf") and ok("UP.PDB") and ok("link_inside.pdb") and ok("meta.json")
    for bad in ("link_out.pdb", "linkdir/secret.pdb", "linkdir/secret.json", ".hid.pdb", "dir.pdb", "note.txt", "../outside/secret.pdb",
                "sub/../../outside/secret.pdb", "ok.pdb\x00", "nope.pdb", "", "/ok.pdb", "sub\\ok2.sdf", "structures/.", "sub//ok2.sdf"):
        assert not ok(bad), bad


# =========================================================================================================== SSE
def _ids(app, c):
    rid = c.post("/lead/api/runs", {"seed_smiles": NITRO}).json()["run_id"]
    return rid, app.state.lead.mgr


@pytest.mark.parametrize("value", [b"", b"abc", b"-1", b"0", b"7", b"99999999", b"123456789012345678901234567890", b" 5", b"5 ", b"1;2", b"1e3",
                                   b"\x00", b"%s%s%s", b"../..", b"9" * 8000])
def test_last_event_id_values_never_crash_the_stream_route(lead, value):
    c = signed_in(lead)
    rid, mgr = _ids(lead, c)
    status, body, exc = raw_asgi(lead, "GET", f"/lead/api/runs/{rid}/events", [cookie_header(c), (b"last-event-id", value)])
    assert exc is None and status == 200 and body.startswith(b"retry: 3000")
    assert mgr._streams == {}                                  # the slot of a finished stream is released


@pytest.mark.xfail(strict=True, reason="F-140 LOW: 'Last-Event-ID: <non-ASCII digit>' (e.g. U+00B2) passes str.isdigit() but int() raises ValueError AFTER open_stream() took the "
                                        "per-user slot: 500, and the slot is never released (4 such requests lock the user out of SSE until the process restarts)")
def test_a_malformed_last_event_id_neither_500s_nor_leaks_a_stream_slot(lead):
    c = signed_in(lead)
    rid, mgr = _ids(lead, c)
    status, _body, _exc = raw_asgi(lead, "GET", f"/lead/api/runs/{rid}/events", [cookie_header(c), (b"last-event-id", b"\xb2")])
    leaked = sum(mgr._streams.values())
    assert status != 500 and leaked == 0


def test_the_per_user_stream_cap_holds_and_slots_are_returned(lead):
    c = signed_in(lead)
    mgr = lead.state.lead.mgr
    uid = c.uid
    assert [mgr.open_stream(uid) for _ in range(5)] == [True] * 4 + [False]
    other = uuid.uuid4()
    assert mgr.open_stream(other)                            # another user is unaffected
    for _ in range(4):
        mgr.close_stream(uid)
    mgr.close_stream(uid)                                    # extra close never goes negative
    assert mgr._streams.get(uid, 0) == 0 and mgr.open_stream(uid)


# ================================================================================ hostile input answers 4xx, not 500
BAD_NAMES = ["a\nb", "a\x00b", "a\tb", "a\x1bb", "a\x7fb", "\u0085", "a\rb"]


@pytest.mark.xfail(strict=True, reason="F-141 LOW: a profile name with a control character (LF, TAB, NUL, ESC, DEL, CR) passes ProfileBody validation and then violates the "
                                        "table CHECK (name !~ '[[:cntrl:]]') or the NUL rule: 500 instead of 422. Saved-run titles are sanitised; profile names are not")
def test_control_characters_in_a_profile_name_are_a_422_not_a_500(lead):
    c = signed_in(lead)
    for nm in BAD_NAMES:
        r = c.post("/lead/profiles", {"name": nm})
        assert r.status_code in (201, 422), (repr(nm), r.status_code)


def test_profile_name_rules_that_hold(lead):
    c = signed_in(lead)
    for nm, code in (("", 422), ("   ", 422), ("x" * 81, 422), ("ok name", 201), ("<img src=x onerror=1>", 201), ("a‮b", 201)):
        assert c.post("/lead/profiles", {"name": nm}).status_code == code, repr(nm)
    raw = c.http.post("/lead/profiles", content=b'{"name":"x","objectives":{"qed":{"weight":NaN}}}', headers={**c.h(), "content-type": "application/json"})
    assert raw.status_code == 422 and "Traceback" not in raw.text
    for body in ({"name": "x", "avoid_smarts": ["((("]}, {"name": "x", "avoid_smarts": ["C"] * 21}, {"name": "x", "avoid_smarts": ["C" * 201]},
                 {"name": "x", "edit_allowlist": ["no_such_edit"]}, {"name": "x", "objectives": {"qed": {"weight": 11}}},
                 {"name": "x", "objectives": {"k" * 41: {"weight": 1}}}, {"name": "x", "avoid_smarts": [""]}, {"name": "x", "avoid_smarts": [5]}, ["x"], "x", 5, None):
        r = c.post("/lead/profiles", body)
        assert r.status_code == 422, (body, r.status_code)
        assert "Traceback" not in r.text


def _events(c, run_json_patch=None):
    rid = c.post("/lead/api/runs", {"seed_smiles": NITRO}).json()["run_id"]
    evs = [json.loads(ln[6:]) for ln in c.get(f"/lead/api/runs/{rid}/events").text.splitlines() if ln.startswith("data: ")]
    if run_json_patch:
        for e in evs:
            if e["type"] == "run_start":
                run_json_patch(e["payload"]["config"])
    return evs


@pytest.mark.xfail(strict=True, reason="F-142 LOW: saving a run whose run_start config carries a target_id that is 'valid' for RunConfig (<= 64 chars, any characters, or '') but not for the "
                                        "table CHECK (^[A-Za-z0-9_-]+$) is a 500 instead of a 422 (the request body's own target_id has the pattern, the embedded one does not)")
def test_saving_a_run_with_an_odd_embedded_target_id_is_a_422_not_a_500(lead):
    c = signed_in(lead)
    for tid in ("bad id!", "", "a/b", "x\n"):
        r = c.post("/lead/library/runs", {"events": _events(c, lambda cfg, t=tid: cfg.update(target_id=t))})
        assert r.status_code in (201, 422), (tid, r.status_code)


def test_library_inputs_that_must_be_refused_are_refused(lead):
    c = signed_in(lead)
    evs = _events(c)
    assert c.post("/lead/library/runs", {"events": evs}).status_code == 201
    for body in ({"events": []}, {"events": evs[:1] * 5001}, {"events": [{"type": "run_start", "run_id": "r", "seq": 0, "payload": {"config": 1}}]},
                 {"events": evs, "stage": "manual"}, {"events": evs, "title": "t" * 121}, {"events": evs, "target_id": "a b"}, {"events": evs, "extra": 1},
                 {"events": [{"type": "nope", "run_id": "r", "seq": 0, "payload": {}}]}):
        r = c.post("/lead/library/runs", body)
        assert r.status_code in (413, 422), (list(body), r.status_code)
    title_ctl = c.post("/lead/library/runs", {"events": evs, "title": "a\x00b\nc"})
    assert title_ctl.status_code == 201 and title_ctl.json()["title"] == "abc"
    big = json.loads(json.dumps(evs))
    big[0]["payload"]["seed_molblock"] = "A" * 1_600_000
    assert c.post("/lead/library/runs", {"events": big}).status_code in (413, 422)
    huge = c.http.post("/lead/library/runs", content=b'{"events":[' + b"0," * 1_100_000 + b"0]}",
                       headers={**c.h(), "content-type": "application/json"})
    assert huge.status_code == 413                             # the app-wide 2 MB body guard
    for step in ({"parent_smiles": "CCO", "child_smiles": "CCN", "edit_id": "x y", "site": [0]}, {"parent_smiles": "CCO", "child_smiles": "(((", "edit_id": "x", "site": [0]},
                 {"parent_smiles": "CCO", "child_smiles": "CCN", "edit_id": "x", "site": [-1]}, {"parent_smiles": "CCO", "child_smiles": "CCN", "edit_id": "x", "site": [10_001]},
                 {"parent_smiles": "CCO", "child_smiles": "CCN", "edit_id": "x", "site": list(range(65))},
                 {"parent_smiles": "CCO", "child_smiles": "CCN", "edit_id": "x", "site": [0], "reward": 1e7}):
        r = c.post("/lead/library/manual", {"steps": [step]})
        assert r.status_code == 422, (step, r.status_code)
    assert c.post("/lead/library/manual", {"steps": [{"parent_smiles": "CCO", "child_smiles": "CCN", "edit_id": "x", "site": [0]}] * 501}).status_code == 422
    assert c.post("/lead/library/manual", {"jsonl": "not json\n"}).status_code == 422
    assert c.post("/lead/library/manual", {"jsonl": "\n" * 10}).status_code == 422
    assert c.post("/lead/library/manual", {"steps": [], "jsonl": None}).status_code == 422


def test_molecule_inputs_that_are_hostile_are_refused_quickly(lead):
    c = signed_in(lead)
    t0 = time.monotonic()
    cases = [{"smiles": "C" * 2000}, {"smiles": "c1ccccc1" * 250}, {"smiles": "C" * 2001}, {"smiles": "C\nC"}, {"smiles": "C C"}, {"smiles": "\x00"},
             {"smiles": "[U+]" * 100}, {"smiles": "C%99" * 400}, {"molfile": "A" * 150_001}, {"molfile": "\n\n\n  0  0\nM  END\n"}, {"molfile": "x\x00M  END"},
             {"smiles": ""}, {}, {"smiles": None, "molfile": None}, {"smiles": "[H][H]"}, {"smiles": "C" * 61}]
    for body in cases:
        r = c.post("/lead/api/molecules/validate", body)
        assert r.status_code in (200, 422), (list(body), r.status_code)
        if r.status_code == 200:
            assert r.json()["ok"] is False, list(body)
        assert "Traceback" not in r.text
    assert time.monotonic() - t0 < 20
    assert c.post("/lead/api/molecules/validate", {"smiles": NITRO}).json()["ok"] is True
    raw = c.http.post("/lead/api/runs", content=b'{"seed_smiles":"CCO","policy_guidance":Infinity}', headers={**c.h(), "content-type": "application/json"})
    assert raw.status_code == 422
    for body in ({"seed_smiles": NITRO, "locked_atoms": [10_000]}, {"seed_smiles": NITRO, "locked_atoms": [-1]}, {"seed_smiles": NITRO, "objectives": {"nope": {}}},
                 {"seed_smiles": NITRO, "adapter_id": "a" * 16}, {"seed_smiles": NITRO, "stage": "manual"}, {"seed_smiles": NITRO, "stage": "binding", "target_id": "egfr"},
                 {"seed_smiles": NITRO, "profile_id": "not-a-uuid"}, {"seed_smiles": NITRO, "profile_id": str(uuid.uuid4())}, {"seed_smiles": NITRO, "target_id": "../x"},
                 {"seed_smiles": NITRO, "generations": 0}, {"seed_smiles": NITRO, "population_size": 10**9}):
        r = c.post("/lead/api/runs", body)
        assert r.status_code in (404, 422, 503), (body, r.status_code)
    for tid in ("egfr\n", "index", "..", "%2e", "x" * 65, "a b"):
        assert c.get("/lead/api/targets/" + __import__("urllib.parse").parse.quote(tid, safe="")).status_code in (404, 422), tid
        assert c.post("/lead/api/poses", {"target_id": tid, "smiles": "CCO"}).status_code in (404, 422), tid


# ===================================================================================== caps, races, isolation
def test_the_per_user_profile_cap_holds_under_concurrent_requests(lead, su4):
    c = signed_in(lead)
    hdr = c.h()
    jar = dict(c.http.cookies)

    def one(i):
        cl = TestClient(lead, base_url="https://testserver", raise_server_exceptions=False)
        cl.cookies.update(jar)
        return cl.post("/lead/profiles", json={"name": f"p{i}"}, headers=hdr).status_code

    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(one, range(26)))
    assert codes.count(201) == 20 and set(codes) <= {201, 409, 429}, codes
    assert su4.execute("SELECT count(*) FROM lead_profile").fetchone()[0] == 20
    assert c.get("/lead/profiles").status_code == 200 and len(c.get("/lead/profiles").json()["profiles"]) == 20


def test_pose_jobs_belong_to_their_owner(lead):
    a, b = signed_in(lead, "a@example.org"), signed_in(lead, "b@example.org")
    erlotinib = "C#Cc1cccc(Nc2ncnc3cc(OCCOC)c(OCCOC)cc23)c1"
    ja = a.post("/lead/api/poses", {"target_id": "egfr", "smiles": erlotinib})
    assert ja.status_code == 200 and ja.json()["source"] == "precomputed"
    jid = ja.json()["job_id"]
    assert a.get(f"/lead/api/poses/{jid}").status_code == 200
    assert b.get(f"/lead/api/poses/{jid}").status_code == 404
    assert b.get(f"/lead/api/poses/{uuid.uuid4()}").status_code == 404
    jb = b.post("/lead/api/poses", {"target_id": "egfr", "smiles": erlotinib}).json()["job_id"]
    assert jb != jid and a.get(f"/lead/api/poses/{jb}").status_code == 404


def test_adapt_jobs_and_library_sessions_belong_to_their_owner(lead, monkeypatch):
    from leadstudio.adapt.jobs import JobManager
    from leadstudio.server import routes

    a, b = signed_in(lead, "a@example.org"), signed_in(lead, "b@example.org")
    st = lead.state.lead
    st.engine = object()                                          # "live" for the route's source check
    monkeypatch.setattr(routes.importlib.util, "find_spec", lambda name, *a_, **k: object())
    lead.state.lead_adapt = mgr = JobManager(lambda *a_, **k: {"ok": True})
    mgr.jobs["jobA"] = {"job_id": "jobA", "owner": str(a.uid), "status": "done", "stage": "done", "progress": 1.0,
                        "result": {"secret": 1}, "error": None, "payload": None, "created": 0}
    assert a.get("/lead/api/adapt/jobs/jobA").status_code == 200
    assert b.get("/lead/api/adapt/jobs/jobA").status_code == 404
    step = {"parent_smiles": NITRO, "child_smiles": ASPIRIN, "edit_id": "x", "site": [0]}
    sid = a.post("/lead/library/manual", {"steps": [step]}).json()["id"]
    assert b.post("/lead/api/adapt/jobs", {"manual_session_id": sid}).status_code == 404
    assert b.post("/lead/api/adapt/jobs", {"manual_session_id": str(uuid.uuid4())}).status_code == 404
    assert b.post("/lead/api/adapt/jobs", {"manual_session_id": sid, "transitions": []}).status_code == 422
    assert b.post("/lead/api/adapt/jobs", {}).status_code == 422
    assert b.post("/lead/api/adapt/jobs", {"transitions": [{}] * 4}).status_code == 422          # below the minimum
    assert b.post("/lead/api/adapt/jobs", {"transitions": [{}] * 2001}).status_code == 422       # above the cap
    assert b.post("/lead/api/adapt/jobs", {"transitions": [{}] * 5, "extra": 1}).status_code == 422


def test_one_users_cancel_does_not_touch_anothers_run(lead):
    a, b = signed_in(lead, "a@example.org"), signed_in(lead, "b@example.org")
    rid = a.post("/lead/api/runs", {"seed_smiles": NITRO}).json()["run_id"]
    run = lead.state.lead.mgr.runs[rid]
    assert b.delete(f"/lead/api/runs/{rid}").status_code == 404 and run.status != "cancelled"
    assert b.delete(f"/lead/api/runs/{'x' * 65}").status_code == 422


# ============================================================================================ logging and leaks
def test_no_structure_name_or_title_reaches_any_log_record(lead, caplog):
    secret_smiles = "CC(C)(C)c1ccc(cc1)C(=O)NC1CCCCC1"
    marker = "C(=O)NC1CCCCC1"
    title, pname = "SECRETTITLE-xyzzy", "SECRETPROFILE-xyzzy"
    caplog.set_level(logging.DEBUG)
    c = signed_in(lead)
    c.post("/lead/api/molecules/validate", {"smiles": secret_smiles})
    c.post("/lead/api/molecules/validate", {"smiles": secret_smiles + "((("})
    rid = c.post("/lead/api/runs", {"seed_smiles": secret_smiles}).json()["run_id"]
    evs = [json.loads(ln[6:]) for ln in c.get(f"/lead/api/runs/{rid}/events").text.splitlines() if ln.startswith("data: ")]
    c.get("/lead/api/manual/applicable", params={"smiles": secret_smiles})
    c.post("/lead/api/manual/preview", {"smiles": secret_smiles, "edit_id": "nope", "site": [0]})
    sess = c.post("/lead/api/manual/sessions", {"seed_smiles": secret_smiles}).json()["session_id"]
    c.post(f"/lead/api/manual/sessions/{sess}/steps", {"parent_smiles": secret_smiles, "child_smiles": ASPIRIN, "edit_id": "x", "site": [0]})
    c.post("/lead/api/poses", {"target_id": "egfr", "smiles": secret_smiles})
    c.post("/lead/library/runs", {"events": evs, "title": title})
    c.post("/lead/library/manual", {"steps": [{"parent_smiles": secret_smiles, "child_smiles": ASPIRIN, "edit_id": "x", "site": [0]}]})
    c.post("/lead/profiles", {"name": pname, "avoid_smarts": [marker]})
    c.post("/lead/profiles", {"name": pname, "avoid_smarts": ["(((" + marker]})
    c.post("/lead/profiles/check-smarts", {"patterns": [marker, "((("]})
    text = "\n".join(f"{r.name}|{r.getMessage()}|{r.exc_text or ''}" for r in caplog.records)
    for needle in (marker, secret_smiles, title, pname):
        assert needle not in text, needle
    assert any(r.name.startswith("leadstudio") for r in caplog.records)      # the logger was live, so the absence above means something


def test_a_server_error_body_is_generic(lead):
    c = signed_in(lead)
    r = c.post("/lead/profiles", {"name": "a\nb"})            # currently a 500 (F-141): whatever the status, the body must not explain
    assert r.status_code in (201, 422, 500)
    for needle in ("Traceback", "psycopg", "sqlalchemy", "/Users/", "CheckViolation", "lead_profile", "constraint", "SELECT", "INSERT"):
        assert needle not in r.text


def test_health_does_not_expose_paths_urls_or_secrets(lead):
    c = signed_in(lead)
    text = c.get("/lead/api/health").text
    for needle in ("/Users/", "/home/", "/app/", "postgres", "METIS_", "STUDIO_", "http://", "https://", "password", "secret"):
        assert needle not in text, needle


# ============================================================================== state held in process memory
def test_the_in_memory_run_registry_is_bounded_per_user_and_overall(mk4):
    app = mk4()
    app.state.lead = mock_state(per_user_active=500, max_runs_per_user_kept=5, max_runs_kept=12)
    mgr = app.state.lead.mgr
    clients = [signed_in(app, f"u{i}@example.org") for i in range(4)]
    for c in clients:
        for _ in range(9):
            assert c.post("/lead/api/runs", {"seed_smiles": NITRO}).status_code == 200
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and any(not r.finished for r in list(mgr.runs.values())):
        time.sleep(0.05)
    assert len(mgr.runs) <= 12
    per_owner: dict = {}
    for r in mgr.runs.values():
        per_owner[r.owner] = per_owner.get(r.owner, 0) + 1
    assert max(per_owner.values()) <= 5


def test_live_manual_sessions_are_bounded_per_user_and_overall(lead):
    c = signed_in(lead)
    sessions = lead.state.lead.manual.sessions
    sessions.per_user = 4
    for _ in range(sessions.per_user + 5):
        assert c.post("/lead/api/manual/sessions", {"seed_smiles": NITRO}).status_code == 200
    assert len(sessions.items) == sessions.per_user
    sid = c.post("/lead/api/manual/sessions", {"seed_smiles": NITRO}).json()["session_id"]
    step = {"parent_smiles": NITRO, "child_smiles": ASPIRIN, "edit_id": "x", "site": [0]}
    sessions.max_steps = 3
    codes = [c.post(f"/lead/api/manual/sessions/{sid}/steps", step).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


@pytest.mark.xfail(strict=True, reason="F-143 LOW: DELETE /account/data removes the database rows only. The user's runs (SSE event lists with molblocks), live manual sessions, pose "
                                        "jobs (ligand/protein payloads) and adapt jobs stay in process memory until eviction or restart, and adapter files under STUDIO_CACHE_DIR "
                                        "are never removed: nothing in the deletion path knows about Lead Studio state")
def test_account_deletion_clears_the_users_in_memory_lead_studio_state(lead):
    c = signed_in(lead)
    uid = c.uid
    st = lead.state.lead
    c.post("/lead/api/runs", {"seed_smiles": NITRO})
    c.post("/lead/api/manual/sessions", {"seed_smiles": NITRO})
    c.post("/lead/api/poses", {"target_id": "egfr", "smiles": "C#Cc1cccc(Nc2ncnc3cc(OCCOC)c(OCCOC)cc23)c1"})
    assert any(r.owner == uid for r in st.mgr.runs.values()) and st.poses.owners
    assert c.delete("/account/data", {"password": S.PW}).status_code == 200
    left = (sum(r.owner == uid for r in st.mgr.runs.values()), sum(o == uid for o, _ in st.manual.sessions.items.values()),
            sum(o == uid for o in st.poses.owners.values()))
    assert left == (0, 0, 0), left


@pytest.mark.xfail(strict=True, reason="F-144 LOW: PoseManager keeps up to 50 jobs PER USER (each holds a full pose payload; precomputed ones are re-read from disk per job) and has no "
                                        "process-wide cap on finished jobs, only a 1 h TTL purged on access: memory grows linearly with the number of active accounts per hour")
def test_pose_job_memory_has_a_process_wide_bound(lead):
    from leadstudio.server.poses import MAX_ACTIVE, PoseManager

    pm = PoseManager(lead.state.lead.svc)
    smiles = "C#Cc1cccc(Nc2ncnc3cc(OCCOC)c(OCCOC)cc23)c1"
    for _ in range(300):
        pm.submit(uuid.uuid4(), "egfr", smiles)
    assert len(pm.jobs) <= max(MAX_ACTIVE * 10, 100), len(pm.jobs)


# ================================================================================================= run timeout
@pytest.mark.xfail(strict=True, reason="F-145 LOW: STUDIO_RUN_TIMEOUT only marks the run finished; the pool thread stays inside the engine until the engine yields. One engine step that does "
                                        "not return (see F-146 and F-162) permanently removes a worker (STUDIO_MAX_CONCURRENT=2 by default), and new runs wait 'queued' behind it")
def test_a_timed_out_run_frees_its_worker():
    from leadstudio.server.core import RunManager, Settings

    release = threading.Event()

    class Hung:
        def run(self, run_id, config):
            yield {"type": "run_start", "payload": {}}
            release.wait(30)                                       # an engine step that does not come back
            yield {"type": "run_end", "payload": {}}

    mgr = RunManager(Settings(max_concurrent=1, run_timeout=0.3, per_user_active=5), Hung(), [])
    from leadstudio.schema import RunConfig

    cfg = RunConfig(seed_smiles="CCO")
    try:
        r1 = mgr.submit(uuid.uuid4(), cfg, "r1")
        deadline = time.monotonic() + 5
        while not r1.finished and time.monotonic() < deadline:
            time.sleep(0.05)
        assert r1.finished                                         # the timeout fired
        r2 = mgr.submit(uuid.uuid4(), cfg, "r2")
        time.sleep(1.5)
        assert r2.status != "queued"                               # a free worker would have started it
    finally:
        release.set()
        mgr.pool.shutdown(wait=True)


def test_the_run_timeout_and_active_run_limits_hold_for_well_behaved_engines():
    from leadstudio.schema import RunConfig
    from leadstudio.server.core import Rejected, RunManager, Settings

    release = threading.Event()

    class Slow:
        def run(self, run_id, config):
            yield {"type": "run_start", "payload": {}}
            release.wait(30)
            yield {"type": "run_end", "payload": {}}

    mgr = RunManager(Settings(max_concurrent=1, max_queue=1, per_user_active=1, run_timeout=60), Slow(), [])
    cfg = RunConfig(seed_smiles="CCO")
    u1, u2, u3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        mgr.submit(u1, cfg, "a")
        with pytest.raises(Rejected) as e1:
            mgr.submit(u1, cfg, "b")                              # per-user limit
        assert e1.value.status_code == 429
        mgr.submit(u2, cfg, "c")
        with pytest.raises(Rejected) as e2:
            mgr.submit(u3, cfg, "d")                              # process-wide queue limit
        assert e2.value.status_code == 429 and e2.value.headers["Retry-After"].isdigit()
    finally:
        release.set()
        mgr.pool.shutdown(wait=True)
