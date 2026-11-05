"""Wave 4: is the repository safe to make public? Secrets, personal data, home paths, hostnames, large or binary blobs, .gitignore coverage and
the licence inventory, all checked against the files git tracks (not the working directory, so local clutter cannot hide or fake a result)."""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
BINARY_OK = {"leadstudio/sa/data/fpscores.pkl.gz": 4_000_000, "web/vendor/ketcher/static/js/main.e47c48ad.js": 30_000_000,
             "web/vendor/ketcher/static/js/127.aa15387f.chunk.js": 1_300_000}


def tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p and (ROOT / p).is_file()]


def text_of(rel: str) -> str | None:
    try:
        data = (ROOT / rel).read_bytes()
    except OSError:
        return None
    return None if b"\0" in data[:4096] else data.decode("utf-8", "replace")


def scan(pattern: str, skip=(), only=None, flags=0) -> list[tuple[str, int, str]]:
    rx, hits = re.compile(pattern, flags), []
    for rel in tracked():
        if any(rel.startswith(s) for s in skip) or (only and not any(rel.startswith(o) for o in only)):
            continue
        t = text_of(rel)
        if t is None:
            continue
        for n, line in enumerate(t.splitlines(), 1):
            m = rx.search(line)
            if m:
                hits.append((rel, n, m.group(0)[:60]))
    return hits


VENDOR = ("web/vendor/",)


# =================================================================================================== secrets
SECRET_PATTERNS = {
    "private key block": r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY",
    "aws access key id": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "github token": r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
    "slack token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b",
    "huggingface token": r"\bhf_[A-Za-z0-9]{30,}\b",
    "openai/anthropic style key": r"\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}\b",
    "google api key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "jwt": r"\beyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}",
    "url with password": r"://[^/\s:@'\"]+:(?!<set-me|\{|\$|%s|password@|pw@|x@|pass@|secret@|test-only|local-demo-only|ci-only|\*\*\*)[^/\s:@'\"]{6,}@(?!example)",
    "bearer literal": r"[Bb]earer\s+[A-Za-z0-9._-]{30,}",
}


@pytest.mark.parametrize("what", sorted(SECRET_PATTERNS))
def test_no_secret_shaped_string_is_tracked(what):
    hits = scan(SECRET_PATTERNS[what], skip=VENDOR + ("tests/",))
    assert hits == [], hits[:5]


def test_assignments_of_secret_looking_names_hold_placeholders_only():
    rx = r"(?i)\b(?:secret_key|api_key|apikey|access_key|secret_access_key|password|passwd|token)\s*[:=]\s*[\"']([^\"'$\s{<][^\"'\s]{11,})[\"']"
    hits = [h for h in scan(rx, skip=VENDOR + ("tests/", "docs/agent_prompts/")) if "-only" not in h[2]]       # "local-demo-only": the throw-away demo database
    assert hits == [], hits[:5]


def test_env_files_other_than_the_example_are_not_tracked_and_the_example_is_placeholders():
    files = tracked()
    assert [f for f in files if re.search(r"(^|/)\.env(\..+)?$", f) and not f.endswith(".env.example")] == []
    for line in (ROOT / "deploy/.env.example").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, val = line.partition("=")
            if re.search(r"PASSWORD|SECRET|KEY|TOKEN|DATABASE_URL|MIGRATION_URL|REDIS_URL", key):
                assert "<set-me" in val, line


def test_gitleaks_allowlist_is_narrow():
    cfg = (ROOT / ".gitleaks.toml").read_text()
    assert "useDefault = true" in cfg
    assert "paths" not in cfg.split("[allowlist]")[1] and cfg.count("'''") <= 8     # three regexes, no path allow-listing


# ============================================================================== personal data, paths, hosts
def test_no_home_directory_paths_in_product_code_data_web_or_deploy():
    hits = scan(r"/Users/[A-Za-z0-9._-]+|/home/[a-z][a-z0-9_-]+/|C:\\Users\\", skip=VENDOR + ("tests/", "docs/", "scripts/"))
    assert hits == [], hits[:5]


@pytest.mark.xfail(strict=True, reason="F-147 LOW: personal absolute paths (macOS user name, local worktree layout) are committed in docs/agent_prompts/*.md and scripts/make_depictions.py "
                                        "(the prompts also name sibling private checkouts). Harmless to run, but it publishes the account name and directory layout of the author")
def test_no_home_directory_paths_in_docs_or_scripts():
    hits = scan(r"/Users/[A-Za-z0-9._-]+|/home/[a-z][a-z0-9_-]+/|C:\\Users\\", only=("docs/", "scripts/"))
    assert hits == [], sorted({h[0] for h in hits})


def test_no_ip_addresses_or_internal_hostnames_are_tracked():
    ip = r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])"
    ok = {"127.0.0.1", "0.0.0.0", "127.0.0.2", "169.254.169.254", "10.0.0.5", "192.168.1.10", "172.16.0.1", "8.8.8.8", "255.255.255.255", "203.0.113.7", "192.0.2.1"}
    ips = [h for h in scan(ip, skip=VENDOR + ("tests/", "leadstudio/data/", "ml/data/")) if h[2] not in ok and not re.search(r"\d+\.\d+\.\d+\.\d+$", h[0])]
    # version-like quads in lock/requirement/metadata files are not addresses
    ips = [h for h in ips if not h[0].endswith((".txt", ".toml", ".lock", ".json", ".yml", ".yaml", ".md"))]
    assert ips == [], ips[:5]
    host = (r"(?i)\b(?:[a-z0-9-]+\.)+(?:internal|lan|corp|intranet|home\.arpa)\b|\b(?:ec2|compute)-[\w.-]*amazonaws\.com|\.compute\.amazonaws\.com|"
            r"\b[\w-]+\.(?:ngrok|trycloudflare|loca\.lt|tailscale\.net|ts\.net|runpod\.io|vast\.ai)\b|\bssh\s+[\w.-]+@[\w.-]+")
    assert scan(host, skip=VENDOR + ("tests/",)) == []


def test_no_personal_email_addresses_are_tracked():
    mails = scan(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", skip=VENDOR + ("tests/",))
    odd = [h for h in mails if not re.search(r"@(?:example\.(?:com|org|net)|users\.noreply\.github\.com|x\.com|metis\.example|localhost)$", h[2])
           and not re.search(r"@(?:types|babel|rollup|vitejs|emotion|mui)", h[2]) and not h[2].startswith("@")]
    assert odd == [], odd[:5]


def test_no_cookies_session_ids_or_authorization_headers_are_tracked():
    assert scan(r"(?i)(?:^|\s)(?:cookie|set-cookie):\s*[\w-]+=[A-Za-z0-9._~%+/=-]{24,}", skip=VENDOR + ("tests/",)) == []
    assert scan(r"(?i)authorization:\s*(?:basic|bearer)\s+[A-Za-z0-9._~+/=-]{16,}", skip=VENDOR + ("tests/",)) == []
    assert scan(r"(?i)\bmetis_session=[A-Za-z0-9._-]{30,}", skip=("tests/",)) == []


def test_git_history_has_one_author_identity_and_no_committed_env_file():
    log = subprocess.run(["git", "log", "--all", "--format=%an <%ae>"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
    assert len({ln for ln in log if ln}) == 1                   # one identity; note it is a personal mail address (INFO in the report)
    names = subprocess.run(["git", "log", "--all", "--name-only", "--format="], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
    assert [n for n in set(names) if re.search(r"(^|/)(\.env(\.[^e].*)?|id_rsa.*|.*\.pem|.*\.key|.*\.p12|.*\.pfx|\.netrc|credentials\.json)$", n)] == []


# ============================================================================================ blobs and .gitignore
def test_tracked_binary_and_large_files_are_the_known_ones():
    big = {}
    for rel in tracked():
        size = (ROOT / rel).stat().st_size
        if size > 1_000_000:
            big[rel] = size
    assert set(big) <= set(BINARY_OK), sorted(set(big) - set(BINARY_OK))
    for rel, size in big.items():
        assert size <= BINARY_OK[rel], (rel, size)
    odd = [r for r in tracked() if re.search(r"\.(?:exe|dll|so|dylib|bin|pt|pth|ckpt|safetensors|onnx|npy|npz|pkl|pickle|db|sqlite3?|dump|zip|tar|gz|7z|rar|jar|war)$", r)
           and r != "leadstudio/sa/data/fpscores.pkl.gz"]
    assert odd == [], odd


def test_every_tracked_text_file_is_utf8_without_a_nul_or_bidi_control():
    bad = []
    for rel in tracked():
        if rel.startswith(VENDOR) or rel.startswith("tests/"):         # tests carry deliberate hostile strings
            continue
        raw = (ROOT / rel).read_bytes()
        if b"\0" in raw[:4096]:
            continue
        try:
            t = raw.decode("utf-8")
        except UnicodeDecodeError:
            bad.append((rel, "not utf-8"))
            continue
        if re.search("[\u202a-\u202e\u2066-\u2069]", t):
            bad.append((rel, "bidi control"))
    assert bad == []


@pytest.mark.parametrize("path", [".env", "deploy/.env", ".env.local", "private/qa_wave4.md", "private/anything", "server.pem", "x/server.key", "node_modules/a/b.js",
                                  "__pycache__/x.pyc", ".DS_Store", ".demo-data/db", "a/b.pt", "m.ckpt", ".pytest_cache/x", ".venv/bin/python", "data/x.csv"])
def test_gitignore_covers_the_files_that_must_never_be_committed(path):
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
    assert r.returncode == 0, path


@pytest.mark.xfail(strict=True, reason="F-148 LOW: .gitignore does not cover several things a public repo with a database, object store and local model cache will produce: SQL/pg dumps "
                                        "(*.dump, dump.sql, *.pgdump), *.sqlite/*.db, .cache/ (STUDIO_CACHE_DIR default: per-user adapter sidecars *.json, PLAPT embeddings, HF cache), "
                                        "*.p12/*.pfx, credentials.json/service-account*.json, id_rsa*, *.tfstate, .netrc/.pypirc, .envrc, .aws/")
def test_gitignore_also_covers_backups_local_caches_and_credential_files():
    missing = [p for p in ("dump.sql", "backup.dump", "a.pgdump", "x.sqlite", "x.db", ".cache/studio/adapters/x/a.json", ".cache/huggingface/x", "x.p12", "x.pfx",
                           "credentials.json", "service-account.json", "a/id_rsa", "terraform.tfstate", ".netrc", ".pypirc", ".envrc", ".aws/credentials")
               if subprocess.run(["git", "check-ignore", "-q", p], cwd=ROOT).returncode != 0]
    assert missing == [], missing


# ========================================================================================================= licences
LIC = (ROOT / "docs/LICENSES.md").read_text()
ROWS = [ln for ln in LIC.splitlines() if ln.startswith("|")]


def has_row(*names: str) -> bool:
    return any(all(n.lower() in ln.lower().split("|")[1] for n in names) for ln in ROWS[2:])


def test_licence_rows_that_exist_state_a_licence_and_a_status():
    for ln in ROWS[2:]:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        assert len(cells) >= 3 and cells[0] and cells[1] and cells[2], ln
        assert re.search(r"cleared|pending|excluded|vendored|not installed", cells[2], re.I), ln


def test_the_licences_the_docs_rely_on_are_present_in_the_tree():
    for rel in ("web/vendor/ketcher/LICENSE", "web/vendor/ketcher/NOTICE", "web/vendor/LICENSE-3Dmol.txt", "web/fonts/OFL.txt", "leadstudio/sa/LICENSE.txt"):
        assert (ROOT / rel).is_file() and (ROOT / rel).stat().st_size > 200, rel
    assert "BSD" in (ROOT / "leadstudio/sa/LICENSE.txt").read_text()
    assert "Open Font License" in (ROOT / "web/fonts/OFL.txt").read_text()
    assert "LGPL" in LIC and "deap" in LIC.lower()
    req = (ROOT / "requirements.txt").read_text()
    assert re.search(r"^deap==1\.4\.4\s+# LGPL-3\.0", req, re.M)


def test_the_installed_lgpl_dependency_chain_is_what_licenses_md_says():
    from importlib import metadata

    names = {d.metadata["Name"].lower() for d in metadata.distributions()}
    assert "deap" in names and "moocore" in names             # LICENSES.md: deap's dependency moocore is LGPL-2.1-or-later
    assert "torch" not in names and "transformers" not in names     # "not installed": the optional models are not part of the shipped image


@pytest.mark.xfail(strict=True, reason="F-149 LOW: docs/LICENSES.md has no row for components that ship in the image or in git: moocore (LGPL-2.1+, pulled by deap), pdb2pqr and propka "
                                        "(in requirements.txt), IBM Plex Sans (web/fonts, OFL.txt is there), ChemBERTa weights (downloaded when STUDIO_EMBED=chemberta), "
                                        "DynamicBind and its precomputed poses (leadstudio/data), PLAPT/ProtBert (named as not shipped but no exclusion row), Open Babel and Meeko "
                                        "(dockprep output formats), RCSB PDB structures and ChEMBL (CC BY-SA) SMILES in leadstudio/data/targets, UniProt; the repository itself has no LICENSE file")
def test_licenses_md_covers_everything_that_ships():
    required = {"moocore": ("moocore",), "pdb2pqr": ("pdb2pqr",), "propka": ("propka",), "IBM Plex": ("plex",), "ChemBERTa": ("chemberta",), "DynamicBind": ("dynamicbind",),
                "PLAPT/ProtBert": ("plapt",), "Open Babel": ("open babel",), "Meeko": ("meeko",), "RCSB PDB": ("rcsb",), "ChEMBL": ("chembl",), "fpocket (own row)": ("fpocket",)}
    missing = [k for k, v in required.items() if not has_row(*v)]
    if not any((ROOT / n).is_file() for n in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")):
        missing.append("repository LICENSE file")
    assert missing == [], missing


def test_bundled_data_has_provenance_and_no_unexpected_personal_content():
    import json

    idx = json.loads((ROOT / "leadstudio/data/targets/index.json").read_text())
    for t in idx:
        d = json.loads((ROOT / f"leadstudio/data/targets/{t['id']}.json").read_text())
        assert re.fullmatch(r"[0-9A-Z]{4}", d["pdb_id"]) and all(x.get("source") for x in d["drugs"])
        pdb = (ROOT / "leadstudio/data/structures" / f"{t['id']}.pdb").read_text()
        assert pdb.splitlines()[0].startswith("REMARK") and "RCSB" in pdb.splitlines()[0]
        assert not re.search(r"(?i)/Users/|@|password", pdb)
