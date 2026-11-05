"""Wave 4: Lead Studio internals that need no HTTP stack. SSRF through the docking URL, unsafe deserialisation / dynamic code in the copied
code (static AST scan plus the SHA-256 pin on the one pickle), SMARTS cost, and the unbounded-match stall in profile scoring."""
from __future__ import annotations

import ast
import gzip
import hashlib
import http.server
import pathlib
import pickle
import re
import socketserver
import subprocess
import sys
import textwrap
import threading

import pytest

from leadstudio.server import poses
from leadstudio.server.poses import HttpDynamicBind, PoseServiceError, validate_dock_url

ROOT = pathlib.Path(__file__).resolve().parents[3]
LEAD = ROOT / "leadstudio"


# ================================================================================================== SSRF
LOOPBACK_OK = ["http://127.0.0.1:8100", "http://localhost:8100", "http://[::1]:8100", "http://127.0.0.1", "https://127.0.0.1:8443/", "http://127.0.0.2:1",
               "HTTP://LOCALHOST:8100", None, ""]
REFUSED = [
    "http://169.254.169.254", "http://169.254.169.254:80/latest/meta-data/", "http://[fd00:ec2::254]", "http://10.0.0.5:8100", "http://192.168.1.10:8100",
    "http://172.16.0.1", "http://metadata.google.internal", "http://localhost.evil.example", "http://127.0.0.1.evil.example", "http://evil.example/@127.0.0.1",
    "http://127.0.0.1@evil.example", "http://user:pw@127.0.0.1:8100", "http://:pw@127.0.0.1:8100", "http://0.0.0.0:8100", "http://0x7f.0.0.1", "http://2130706433",
    "http://017700000001", "http://127.1", "http://[::]:8100", "ftp://127.0.0.1", "file:///etc/passwd", "gopher://127.0.0.1:70", "ws://127.0.0.1", "javascript:1",
    "http://127.0.0.1:8100/x", "http://127.0.0.1:8100/..", "http://127.0.0.1:8100?x=1", "http://127.0.0.1:8100#f", "http://", "http:///", "127.0.0.1:8100", "//127.0.0.1",
    "http://127.0.0.1:99999", "http://127.0.0.1:-1", "http://127.0.0.1:abc", "http://127.0.0.1\n.evil.example", "http://evil.example\\@127.0.0.1", "http://[::ffff:10.0.0.1]",
    "http://8.8.8.8", "https://dock.example", "http://dock.example:8100", "   http://evil.example",
]


@pytest.mark.parametrize("url", LOOPBACK_OK, ids=repr)
def test_loopback_docking_urls_are_accepted(url):
    assert validate_dock_url(url).lower().startswith(("http://", "https://"))


@pytest.mark.parametrize("url", REFUSED, ids=repr)
def test_every_other_docking_url_is_refused_without_an_allow_list(url):
    with pytest.raises(PoseServiceError):
        validate_dock_url(url)


def test_the_allow_list_matches_one_exact_origin_and_nothing_near_it():
    allowed = "https://dock.example:8443"
    assert validate_dock_url("https://dock.example:8443", allowed) == "https://dock.example:8443"
    assert validate_dock_url("HTTPS://Dock.Example:8443/", allowed) == "HTTPS://Dock.Example:8443"
    for url in ("http://dock.example:8443", "https://dock.example", "https://dock.example:8444", "https://dock.example.evil.example:8443",
                "https://evildock.example:8443", "https://dock.example:8443/x", "https://u:p@dock.example:8443", "https://dock.example:8443?x", "http://169.254.169.254"):
        with pytest.raises(PoseServiceError):
            validate_dock_url(url, allowed)
    for bad_allowed in (None, "", "dock.example", "https://u:p@dock.example:8443", "https://dock.example:8443/path", "ftp://dock.example:8443", "https://"):
        with pytest.raises(PoseServiceError):
            validate_dock_url("https://dock.example:8443", bad_allowed)
    assert validate_dock_url("http://127.0.0.1:8100", "https://dock.example:8443")        # loopback stays valid whatever the allow-list is


def test_the_pose_client_is_built_from_settings_only(monkeypatch):
    """Nothing in a request can reach the URL: the routes never pass one, and the only env switch is the operator's."""
    src = (LEAD / "server" / "routes.py").read_text()
    assert "HttpDynamicBind" not in src and "dock_url" not in src.replace("lead_dock_url", "") and "validate_dock_url" not in src
    monkeypatch.setenv("STUDIO_DYNAMICBIND_URL", "http://169.254.169.254")
    with pytest.raises(PoseServiceError):
        HttpDynamicBind()
    monkeypatch.setenv("STUDIO_DYNAMICBIND_URL", "http://169.254.169.254")
    pm = poses.PoseManager(svc=None)
    assert pm.client() is None and pm._config_error                            # refused at configuration time, the service is "unavailable"


class _Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass


def _serve(handler):
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_redirects_are_never_followed():
    hits = []

    class Target(_Quiet):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    t_srv, t_url = _serve(Target)

    class Redirect(_Quiet):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", t_url + "/internal")
            self.end_headers()

        do_POST = do_GET

    r_srv, r_url = _serve(Redirect)
    try:
        c = HttpDynamicBind(r_url)
        with pytest.raises(PoseServiceError, match="HTTP 302"):
            c.ping()
        with pytest.raises(PoseServiceError):
            c.dock("egfr", "CCO")
        assert hits == []
    finally:
        r_srv.shutdown()
        t_srv.shutdown()


def test_hostile_service_answers_are_contained():
    answers = {}

    class Hostile(_Quiet):
        def do_GET(self):
            body = answers["body"]
            self.send_response(answers.get("status", 200))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

    srv, url = _serve(Hostile)
    try:
        c = HttpDynamicBind(url)
        answers["body"] = b"x" * (poses.MAX_RESPONSE_BYTES + 10)
        with pytest.raises(PoseServiceError, match="too large"):
            c.ping()
        for raw in (b"not json", b"[1,2]", b"\xff\xfe", b'"str"', b"null"):
            answers["body"] = raw
            with pytest.raises(PoseServiceError):
                c.ping()
        for jid in ("../../etc/passwd", "a/b", "a b", "a?x=1", "a#b", "x" * 65, "", "a\rb", "a%2f", "١٢", ["a"], {"a": 1}):
            answers["body"] = ('{"job_id": %s}' % __import__("json").dumps(jid)).encode()
            with pytest.raises(PoseServiceError):
                c.dock("egfr", "CCO")
        for rid in ("../x", "a/b", "a b", "a?x", "x" * 65, "", "a\r\nHost: evil", "a%00"):
            with pytest.raises(PoseServiceError):
                c.job(rid)
        answers["body"] = b'{"job_id": "abc_DEF-123"}'
        assert c.dock("egfr", "CCO") == "abc_DEF-123"
    finally:
        srv.shutdown()


def test_a_trailing_newline_job_id_cannot_split_the_request():
    """REMOTE_ID uses `$`, which also matches before a final newline. urllib strips the newline from the URL, so the request that goes out is the
    clean one and nothing can be appended. (Latent: use fullmatch or \\Z so the regex alone is enough.)"""
    seen = []

    class Rec(_Quiet):
        def do_GET(self):
            seen.append(self.requestline)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    srv, url = _serve(Rec)
    try:
        c = HttpDynamicBind(url)
        try:
            c.job("abc\n")
        except PoseServiceError:
            pass
        except Exception as exc:  # noqa: BLE001
            assert "control characters" in str(exc)
        assert seen in ([], ["GET /jobs/abc HTTP/1.1"])
        seen.clear()
        for evil in ("abc\r\nX-Evil: 1", "abc\nGET /admin", "abc\n\n"):
            with pytest.raises(PoseServiceError):
                c.job(evil)
        assert seen == []
    finally:
        srv.shutdown()


# ================================================================================ unsafe deserialisation / code
def _py_files():
    return [p for p in LEAD.rglob("*.py") if "__pycache__" not in p.parts]


def _calls(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            yield n


def _dotted(node) -> str:
    if isinstance(node, ast.Attribute):
        return _dotted(node.value) + "." + node.attr
    return node.id if isinstance(node, ast.Name) else ""


def test_no_dynamic_code_execution_or_unsafe_loaders_in_the_copied_code():
    bad_calls = {"eval", "exec", "compile", "__import__", "pickle.load", "pickle.loads", "cPickle.load", "dill.load", "dill.loads", "marshal.load", "marshal.loads",
                 "shelve.open", "joblib.load", "yaml.load", "yaml.unsafe_load", "yaml.full_load", "os.system", "os.popen", "os.execv", "os.execvp", "subprocess.run",
                 "subprocess.Popen", "subprocess.call", "subprocess.check_output", "subprocess.check_call", "numpy.load", "np.load", "torch.jit.load", "importlib.__import__"}
    allowed = {("leadstudio/sa/sa_score.py", "pickle.load")}       # SHA-256 pinned, see test below
    found = []
    for p in _py_files():
        rel = str(p.relative_to(ROOT))
        tree = ast.parse(p.read_text())
        for call in _calls(tree):
            name = _dotted(call.func)
            if name in bad_calls and (rel, name) not in allowed:
                found.append((rel, name, call.lineno))
            if name in ("torch.load",) and not any(k.arg == "weights_only" and isinstance(k.value, ast.Constant) and k.value.value is True for k in call.keywords):
                found.append((rel, "torch.load without weights_only=True", call.lineno))
            if name.endswith("from_pretrained") and not any(k.arg == "revision" for k in call.keywords):
                found.append((rel, "from_pretrained without a pinned revision", call.lineno))
            if any(k.arg in ("trust_remote_code", "allow_pickle") and not (isinstance(k.value, ast.Constant) and k.value.value is False) for k in call.keywords):
                found.append((rel, "trust_remote_code/allow_pickle", call.lineno))
            if name == "importlib.import_module" and call.args and not isinstance(call.args[0], ast.Constant):
                found.append((rel, "import_module with a non-constant name", call.lineno))
    assert found == []


def test_the_only_pickle_is_the_sha256_pinned_sa_table():
    users = [str(p.relative_to(ROOT)) for p in _py_files() if re.search(r"^\s*(import|from)\s+(pickle|cPickle|dill|marshal|shelve|joblib)\b", p.read_text(), re.M)]
    assert users == ["leadstudio/sa/sa_score.py"]
    from leadstudio.sa import sa_score as m

    assert hashlib.sha256(m._DATA_FILE.read_bytes()).hexdigest() == m._DATA_SHA256


def test_a_tampered_sa_table_is_refused_before_unpickling(tmp_path, monkeypatch):
    from leadstudio.sa import sa_score as m

    marker = tmp_path / "pwned"

    class Evil:
        def __reduce__(self):
            return (open, (str(marker), "w"))

    evil = tmp_path / "fpscores.pkl.gz"
    evil.write_bytes(gzip.compress(pickle.dumps(Evil())))
    monkeypatch.setattr(m, "_DATA_FILE", evil)
    monkeypatch.setattr(m, "_fscores", None)
    with pytest.raises(RuntimeError, match="integrity"):
        m.SyntheticAccessibility.readFragmentScores()
    assert not marker.exists()


def test_model_downloads_are_pinned_allow_listed_and_weights_load_safely():
    src = (LEAD / "embed" / "chemberta.py").read_text()
    from leadstudio.embed import chemberta

    for repo, rev in chemberta.CHECKPOINTS:
        assert re.fullmatch(r"[0-9a-f]{40}", rev) and repo.startswith(("DeepChem/", "seyonec/"))
    assert "trust_remote_code" not in src and "use_auth_token" not in src and "token=" not in src
    assert "from_pretrained(repo, revision=rev)" in src
    for rel in ("policy/dqn.py", "adapt/policy_factory.py"):
        assert "weights_only=True" in (LEAD / rel).read_text()
    # the checkpoint path is a settings value, never a request value
    routes = (LEAD / "server" / "routes.py").read_text()
    assert "checkpoint" not in routes.lower().replace("checkpoint_loaded", "")


def test_yaml_is_only_ever_read_with_safe_load():
    for p in _py_files():
        for m in re.finditer(r"\byaml\.(\w+)\(", p.read_text()):
            assert m.group(1) in ("safe_load", "safe_dump", "dump", "safe_load_all"), (p, m.group(0))


# ====================================================================================================== SMARTS cost
def test_the_smarts_check_route_helpers_are_cheap_even_for_hostile_patterns():
    import time

    from leadstudio.profiles import MAX_SMARTS, MAX_SMARTS_LEN, check_smarts

    nest = "C"
    while len("[$(" + nest + ")]") <= MAX_SMARTS_LEN:
        nest = "[$(" + nest + ")]"
    pats = [nest, "[" + ",".join(["#6"] * 60) + "]", "[#6;" + "!".join(["R"] * 80) + "]", "*" * MAX_SMARTS_LEN, "C" + "(C)" * 60, "C" * (MAX_SMARTS_LEN + 1),
            "\x00", "[", "[$(", "C%99", "[#6]~" * 40]
    t0 = time.monotonic()
    for _ in range(MAX_SMARTS // len(pats) + 20):
        for p in pats:
            check_smarts(p[:MAX_SMARTS_LEN + 5])
    assert time.monotonic() - t0 < 3.0
    assert check_smarts("C" * (MAX_SMARTS_LEN + 1)) is not None and check_smarts("") is not None and check_smarts("((") is not None


GRID_CODE = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[2])
    from rdkit import Chem
    from leadstudio.profile_scorer import ProfileScorer
    from leadstudio.profiles import ProfileBody
    from leadstudio.schema import RunConfig
    from leadstudio.scoring import RDKitScorer
    from leadstudio.server import molio
    w, h = 6, 10
    m = Chem.RWMol()
    ids = {(i, j): m.AddAtom(Chem.Atom(6)) for i in range(w) for j in range(h)}
    for i in range(w):
        for j in range(h):
            if i + 1 < w: m.AddBond(ids[i, j], ids[i + 1, j], Chem.BondType.SINGLE)
            if j + 1 < h: m.AddBond(ids[i, j], ids[i, j + 1], Chem.BondType.SINGLE)
    mol = m.GetMol(); Chem.SanitizeMol(mol)
    smi = molio.canonical(molio.parse_molecule(Chem.MolToSmiles(mol)))     # the molecule passes the service's own input checks
    prof = ProfileBody(name="p", avoid_smarts=[sys.argv[1]])               # and so does the pattern (ProfileBody runs check_smarts)
    res = ProfileScorer(RDKitScorer(), prof).score(smi, RunConfig(seed_smiles=smi))
    print("done", round(res.composite, 3))
""")


def _score_in_subprocess(smarts: str, timeout: float):
    return subprocess.run([sys.executable, "-I", "-c", GRID_CODE, smarts, str(ROOT)], cwd=ROOT, env={"PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=timeout)


def test_ordinary_avoid_patterns_score_a_large_seed_quickly():
    out = _score_in_subprocess("[#6]~[#6]~[#6]~[#9]", 60)
    assert out.returncode == 0 and out.stdout.startswith("done"), out.stderr[-400:]


@pytest.mark.xfail(strict=True, reason="F-162 MEDIUM: an avoid_smarts pattern passes validation (it is only PARSED) but GetSubstructMatch is exponential on a dense 60-heavy-atom seed that the "
                                        "service also accepts: an 80-character chain of 18 atoms takes ~75 s on one core, 22 atoms effectively never returns. The scorer call sits in "
                                        "the run worker, which STUDIO_RUN_TIMEOUT cannot interrupt (F-145): two such runs by one account stall the Lead Studio engine until restart")
def test_a_valid_avoid_pattern_cannot_stall_scoring():
    smarts = "~".join(["[#6]"] * 22) + "~[#6;R0;R1]"
    assert len(smarts) <= 200
    try:
        out = _score_in_subprocess(smarts, 8)
    except subprocess.TimeoutExpired:
        pytest.fail("scoring one candidate did not return within 8 s")
    assert out.returncode == 0


# ================================================================================================ rate limits
def test_every_mutating_or_cpu_heavy_lead_route_calls_the_shared_limiter():
    tree = ast.parse((LEAD / "server" / "routes.py").read_text())
    heavy_get = {"applicable"}                        # GET but runs the registry over a user molecule
    missing, seen = [], 0
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        verbs = {d.func.attr for d in fn.decorator_list if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and _dotted(d.func.value) in ("api", "library", "profiles")}
        if not verbs:
            continue
        seen += 1
        needs = bool(verbs & {"post", "put"}) or fn.name in heavy_get
        calls = {_dotted(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
        if needs and "throttle" not in calls:
            missing.append(fn.name)
    assert seen >= 30 and missing == []


EMBED_CODE = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[2])
    from rdkit import Chem
    from leadstudio.server import molio
    w, h = 5, 6
    m = Chem.RWMol()
    ids = {(i, j): m.AddAtom(Chem.Atom(6)) for i in range(w) for j in range(h)}
    for i in range(w):
        for j in range(h):
            if i + 1 < w: m.AddBond(ids[i, j], ids[i + 1, j], Chem.BondType.SINGLE)
            if j + 1 < h: m.AddBond(ids[i, j], ids[i, j + 1], Chem.BondType.SINGLE)
    mol = m.GetMol(); Chem.SanitizeMol(mol)
    smi = molio.canonical(molio.parse_molecule(Chem.MolToSmiles(mol)))
    print(len(smi), smi, flush=True)
    block, _ = molio.molblock_3d(smi)                  # what POST /lead/api/molecules/validate does after parsing
    print("done", block is not None)
""")


@pytest.mark.xfail(strict=True, reason="F-146 MEDIUM: 3D embedding has no time limit. A small square-lattice SMILES accepted by parse_molecule needs 4 to 45 s of CPU (load dependent) to FAIL in ETKDG at 16 atoms, "
                                        "and a 30-atom one did not finish within 40 s; 60 atoms is effectively unbounded. It runs in validate/preview (holding one of the 6 HeavyGate slots and a "
                                        "pool thread, 429 for everyone else) and as the first step of every run (a run worker that STUDIO_RUN_TIMEOUT cannot free, F-145)")
def test_embedding_a_small_accepted_molecule_is_time_bounded():
    try:
        out = subprocess.run([sys.executable, "-I", "-c", EMBED_CODE, "x", str(ROOT)], cwd=ROOT, env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        pytest.fail("embedding a 30-atom molecule took more than 5 s")
    assert out.returncode == 0
