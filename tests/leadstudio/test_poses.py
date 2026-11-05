"""Pose jobs: precomputed poses work with no service; a down service is a clear 503; the DynamicBind URL comes only
from settings, is loopback or explicitly allow-listed, and is not an SSRF vector."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import H, make_app
from fastapi.testclient import TestClient

from leadstudio.data import loader
from leadstudio.server import molio, poses
from leadstudio.server.core import Rejected, Settings
from leadstudio.server.poses import HttpDynamicBind, PoseManager, PoseServiceError, validate_dock_url

NOVEL = "CCOC(=O)c1ccc(cc1)[N+](=O)[O-]"  # no precomputed pose for this one


# ---------------------------------------------------------------------------------------------------- URL
@pytest.mark.parametrize("url", ["http://127.0.0.1:8100", "http://localhost:8100", "http://[::1]:8100", "http://127.0.0.1:8100/",
                                 "http://127.1.2.3:9", "https://localhost"])
def test_loopback_urls_are_accepted(url):
    assert validate_dock_url(url).rstrip("/") == url.rstrip("/")


def test_default_is_loopback():
    assert validate_dock_url(None) == "http://127.0.0.1:8100"


@pytest.mark.parametrize("url", [
    "http://10.0.0.5:8100", "http://169.254.169.254/latest/meta-data", "http://evil.example.com", "http://127.0.0.1@evil.example.com",
    "http://user:pw@127.0.0.1:8100", "file:///etc/passwd", "ftp://127.0.0.1", "gopher://127.0.0.1", "http://127.0.0.1:8100/x",
    "http://127.0.0.1:8100/?a=b", "http://127.0.0.1:8100/#f", "", "not a url", "http://", "//127.0.0.1", "http://localhost.evil.com",
    "http://127.0.0.1.evil.com", "http://0.0.0.0:8100", "http://[::ffff:10.0.0.1]:8100"])
def test_other_urls_are_refused(url):
    with pytest.raises(PoseServiceError):
        validate_dock_url(url or "x://")


def test_allow_list_is_exact_origin_only():
    allowed = "https://gpu.internal.example:9443"
    assert validate_dock_url("https://gpu.internal.example:9443", allowed) == "https://gpu.internal.example:9443"
    for other in ("https://gpu.internal.example:9444", "http://gpu.internal.example:9443", "https://gpu.internal.example",
                  "https://other.example:9443", "https://gpu.internal.example.evil.com:9443"):
        with pytest.raises(PoseServiceError):
            validate_dock_url(other, allowed)
    with pytest.raises(PoseServiceError):  # a malformed allow-list entry allows nothing
        validate_dock_url("https://gpu.internal.example:9443", "https://gpu.internal.example:9443/path")


def test_env_url_is_validated(monkeypatch):
    monkeypatch.setenv("STUDIO_DYNAMICBIND_URL", "http://10.1.2.3:8100")
    with pytest.raises(PoseServiceError):
        HttpDynamicBind()
    assert HttpDynamicBind(allowed="http://10.1.2.3:8100").url == "http://10.1.2.3:8100"


def test_setting_is_read_from_metis_env(monkeypatch):
    from backend.core.settings import Settings as MetisSettings
    monkeypatch.setenv("METIS_LEAD_DOCK_URL", "http://10.1.2.3:8100")
    assert MetisSettings().lead_dock_url == "http://10.1.2.3:8100"


# ------------------------------------------------------------------------------------------ real HTTP stub
class Stub:
    def __init__(self, routes):
        outer = self
        self.hits = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self):
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.hits.append((self.command, self.path, body))
                code, payload, headers = routes(self.command, self.path)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _serve

        self.srv = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def stub():
    made = []

    def make(routes):
        s = Stub(routes)
        made.append(s)
        return s

    yield make
    for s in made:
        s.stop()


def ok_service(path_method_state):
    def routes(method, path):
        if path == "/health":
            return 200, {"ok": True}, {}
        if method == "POST" and path == "/dock":
            return 200, {"job_id": "abc123"}, {}
        if path == "/jobs/abc123":
            return 200, {"status": "done", "ligand_sdf": "SDF", "protein_pdb": "PDB", "pose_confidence": 0.7,
                         "affinity": 6.5, "note": "stub service"}, {}
        return 404, {"detail": "nope"}, {}
    return routes


def wait_done(pm, owner, jid, secs=5.0):
    t = time.time()
    while time.time() - t < secs:
        j = pm.get(jid, owner)
        if j.status in ("done", "failed"):
            return j
        time.sleep(0.02)
    raise AssertionError("pose job did not finish")


def test_job_flow_against_a_service(stub, users):
    s = stub(ok_service(None))
    svc = make_app.__globals__["build_state"](settings=Settings(force_mock=True)).svc
    pm = PoseManager(svc, client=HttpDynamicBind(s.url, timeout=2), poll=0.01, timeout=5)
    j = pm.submit(users["a"].id, "egfr", NOVEL)
    assert j.status == "queued" and j.source == "dynamicbind"
    done = wait_done(pm, users["a"].id, j.job_id)
    assert done.status == "done" and done.ligand_sdf == "SDF" and done.pose_confidence == 0.7 and done.apo_pdb
    assert [h[1] for h in s.hits if h[0] == "POST"] == ["/dock"]
    # same (target, molecule) while alive reuses the job
    assert pm.submit(users["a"].id, "egfr", NOVEL).job_id == j.job_id


def test_redirects_are_not_followed(stub):
    target = stub(lambda m, p: (200, {"ok": True}, {}))
    redirector = stub(lambda m, p: (302, {}, {"Location": target.url + "/health"}))
    c = HttpDynamicBind(redirector.url, timeout=2)
    with pytest.raises(PoseServiceError):
        c.ping()
    assert target.hits == []  # the redirect target was never contacted


def test_oversized_and_malformed_responses_are_refused(stub, monkeypatch):
    big = stub(lambda m, p: (200, b"x" * 2000, {}))
    monkeypatch.setattr(poses, "MAX_RESPONSE_BYTES", 1000)
    with pytest.raises(PoseServiceError, match="too large"):
        HttpDynamicBind(big.url).ping()
    bad = stub(lambda m, p: (200, b"not json", {}))
    with pytest.raises(PoseServiceError, match="invalid JSON"):
        HttpDynamicBind(bad.url).ping()
    lst = stub(lambda m, p: (200, b"[1,2]", {}))
    with pytest.raises(PoseServiceError, match="unexpected"):
        HttpDynamicBind(lst.url).ping()


def test_remote_job_id_cannot_inject_a_path(stub):
    evil = stub(lambda m, p: (200, {"job_id": "../../admin"}, {}))
    c = HttpDynamicBind(evil.url)
    with pytest.raises(PoseServiceError, match="job_id"):
        c.dock("egfr", "CCO")
    with pytest.raises(PoseServiceError):
        c.job("../../admin")
    assert [h[1] for h in evil.hits] == ["/dock"]


def test_invalid_remote_result_fails_the_job_not_the_server(stub, users):
    s = stub(lambda m, p: (200, {"job_id": "j1"} if p == "/dock" else {"status": "done", "pose_confidence": {"x": 1}}, {}))
    svc = make_app.__globals__["build_state"](settings=Settings(force_mock=True)).svc
    pm = PoseManager(svc, client=HttpDynamicBind(s.url), poll=0.01, timeout=5)
    j = wait_done(pm, users["a"].id, pm.submit(users["a"].id, "egfr", NOVEL).job_id)
    assert j.status == "failed" and "invalid" in j.note


def test_failed_remote_job_is_reported(stub, users):
    s = stub(lambda m, p: (200, {"job_id": "j1"} if p == "/dock" else {"status": "failed", "note": "no pocket"}, {}))
    svc = make_app.__globals__["build_state"](settings=Settings(force_mock=True)).svc
    pm = PoseManager(svc, client=HttpDynamicBind(s.url), poll=0.01, timeout=5)
    j = wait_done(pm, users["a"].id, pm.submit(users["a"].id, "egfr", NOVEL).job_id)
    assert j.status == "failed" and "no pocket" in j.note


# ------------------------------------------------------------------------------------------- HTTP layer
def test_precomputed_poses_work_with_no_service(client):
    n = 0
    for tid in ("egfr", "hiv1_protease", "abl1", "braf", "fxa"):
        for d in loader.get_target(tid).drugs:
            r = client.post("/lead/api/poses", json={"target_id": tid, "smiles": d.smiles}, headers=H("a"))
            assert r.status_code == 200, (tid, d.name, r.text)
            j = r.json()
            assert j["status"] == "done" and j["source"] == "precomputed" and j["ligand_sdf"] and j["protein_pdb"]
            n += 1
    assert n == 21


def test_every_precomputed_file_belongs_to_a_target_drug():
    for tid in ("egfr", "hiv1_protease", "abl1", "braf", "fxa"):
        files = {p.stem for p in (poses.precomputed_dir() / tid).glob("*.json") if not p.name.startswith("_")}
        want = {poses.smiles_hash(molio.canonical(molio.parse_molecule(d.smiles))) for d in loader.get_target(tid).drugs}
        assert files == want, tid


def test_down_service_is_a_clear_503_without_a_stack_trace(factory, users):
    # nothing listens on this loopback port
    app = make_app(factory, users, pose_client=HttpDynamicBind("http://127.0.0.1:9", timeout=1))
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/lead/api/poses", json={"target_id": "egfr", "smiles": NOVEL}, headers=H("a"))
    assert r.status_code == 503
    body = r.json()
    assert "docking service is not available" in body["detail"] and "Traceback" not in r.text and "127.0.0.1" not in r.text


def test_disallowed_configured_url_means_503_not_a_request(factory, users, monkeypatch):
    monkeypatch.setenv("STUDIO_DYNAMICBIND_URL", "http://169.254.169.254")
    app = make_app(factory, users)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/lead/api/poses", json={"target_id": "egfr", "smiles": NOVEL}, headers=H("a"))
    assert r.status_code == 503 and "169.254" not in r.text


def test_request_cannot_choose_the_service_url(factory, users, stub):
    s = stub(ok_service(None))
    decoy = stub(lambda m, p: (200, {"ok": True}, {}))
    app = make_app(factory, users, pose_client=HttpDynamicBind(s.url, timeout=2))
    c = TestClient(app)
    for extra in ({"url": decoy.url}, {"dynamicbind_url": decoy.url}, {"host": "127.0.0.1", "port": decoy.srv.server_port}):
        r = c.post("/lead/api/poses", json={"target_id": "egfr", "smiles": NOVEL, **extra}, headers=H("a"))
        assert r.status_code in (200, 422), r.text
    for h in ({"X-Forwarded-Host": decoy.url}, {"Host": "127.0.0.1:" + str(decoy.srv.server_port)}):
        c.post("/lead/api/poses", json={"target_id": "egfr", "smiles": NOVEL}, headers={**H("a"), **h})
    time.sleep(0.1)
    assert decoy.hits == []


def test_pose_request_validation(client):
    for bad in ({"target_id": "nope", "smiles": NOVEL}, {"target_id": "egfr", "smiles": "zz"}, {"target_id": "egfr", "smiles": "C" * 2001},
                {"target_id": "e" * 65, "smiles": NOVEL}):
        assert client.post("/lead/api/poses", json=bad, headers=H("a")).status_code == 422, bad


def test_dock_bound_pose_requests_are_rate_limited_per_user(client):
    codes = [client.post("/lead/api/poses", json={"target_id": "egfr", "smiles": NOVEL}, headers=H("b")).status_code
             for _ in range(12)]
    assert codes[:10] == [503] * 10 and codes[10:] == [429, 429]   # 503: no service on loopback in tests
    assert client.post("/lead/api/poses", json={"target_id": "egfr", "smiles": NOVEL}, headers=H("c")).status_code == 503


def test_per_user_and_global_active_job_caps(stub, users):
    gate = threading.Event()

    def routes(m, p):
        if p == "/health":
            return 200, {}, {}
        if m == "POST":
            return 200, {"job_id": "j1"}, {}
        gate.wait(2)
        return 200, {"status": "failed", "note": "x"}, {}

    s = stub(routes)
    svc = make_app.__globals__["build_state"](settings=Settings(force_mock=True)).svc
    pm = PoseManager(svc, client=HttpDynamicBind(s.url), poll=0.01, timeout=5)
    a = users["a"].id
    pm.submit(a, "egfr", "CCO")
    pm.submit(a, "egfr", "CCN")
    with pytest.raises(Rejected) as e:
        pm.submit(a, "egfr", "CCC")
    assert e.value.status_code == 429
    gate.set()
