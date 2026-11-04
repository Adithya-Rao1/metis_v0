"""Static checks over web/: hard design rules, vendoring, fixtures vs frozen contract, legal copies, contrast."""
import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
OWN_TEXT = [p for p in WEB.rglob("*") if p.is_file() and p.suffix in {".html", ".css", ".js", ".svg", ".json"}
            and "vendor" not in p.parts and "screenshots" not in p.parts]
CSS_HTML_JS = [p for p in OWN_TEXT if p.suffix in {".html", ".css", ".js"}]


def test_no_gradients_or_shadows():
    bad = re.compile(r"gradient\s*\(|box-shadow|text-shadow|drop-shadow|filter\s*:\s*blur|<linearGradient|<radialGradient", re.I)
    hits = [(str(p.relative_to(WEB)), m.group(0)) for p in OWN_TEXT for m in bad.finditer(p.read_text())]
    assert hits == []


VERDICT = re.compile(r"\b(safe|unsafe|toxic|nontoxic|non-toxic|good|bad|poor|promising|recommended|avoid|green|amber|red|"
                     r"likely to fail|drug-like|druglike|no-go|dangerous|harmful|harmless)\b", re.I)


def test_no_verdict_words_in_own_pages():
    # legal copies are verbatim docs; fixtures hold only data. Check pages, scripts and styles.
    files = [p for p in CSS_HTML_JS if "legal" not in p.parts and p.name != "legal_text.js"]
    hits = []
    for p in files:
        text = p.read_text()
        text = re.sub(r"prefers-color-scheme|color-scheme", "", text)
        for m in VERDICT.finditer(text):
            hits.append((p.name, m.group(0)))
    assert hits == []


def test_no_status_color_classes():
    css = (WEB / "css" / "app.css").read_text()
    classes = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
    forbidden = {"ok", "warn", "bad", "good", "success", "danger", "error", "fail", "pass", "green", "red", "amber", "yellow", "pill"}
    assert classes & forbidden == set()
    for p in CSS_HTML_JS:
        if p.suffix == ".css":
            continue
        for m in re.finditer(r'class[:=]\s*["\']([^"\']+)["\']', p.read_text()):
            assert not (set(m.group(1).split()) & forbidden), (p.name, m.group(1))


def test_no_runtime_cdn_or_external_urls():
    ok_prefix = ("http://www.w3.org/", "http://www.rdkit.org/")
    hits = []
    for p in CSS_HTML_JS + [p for p in OWN_TEXT if p.suffix == ".svg"]:
        for m in re.finditer(r"https?://[^\s\"')<>]+", p.read_text()):
            if not m.group(0).startswith(ok_prefix):
                hits.append((p.name, m.group(0)))
    assert hits == []


def test_vendor_versions_and_hashes():
    v = (WEB / "vendor" / "VERSIONS.txt").read_text()
    assert "3Dmol.js 2.5.3" in v and "BSD-3-Clause" in v and "SIL Open Font License" in v
    for f in ["vendor/3Dmol-min.js", "fonts/plex-sans-400.woff2", "fonts/plex-sans-500.woff2", "fonts/plex-sans-600.woff2"]:
        h = hashlib.sha256((WEB / f).read_bytes()).hexdigest()
        assert h in v, f
    assert (WEB / "fonts" / "OFL.txt").exists() and (WEB / "vendor" / "LICENSE-3Dmol.txt").exists()


def test_ketcher_is_vendored_with_pinned_version_hashes_and_licence():
    v = (WEB / "vendor" / "VERSIONS.txt").read_text()
    k = WEB / "vendor" / "ketcher"
    assert "Ketcher 3.18.0" in v and "Apache-2.0" in v
    for rel in ["static/js/main.e47c48ad.js", "static/js/127.aa15387f.chunk.js", "static/js/609.d5254829.chunk.js", "static/js/114.cd66d630.chunk.js",
                "static/css/main.96b87be0.css", "index.html", "LICENSE", "NOTICE"]:
        assert hashlib.sha256((k / rel).read_bytes()).hexdigest() in v, rel
    assert "Apache License" in (k / "LICENSE").read_text()[:400] and "Version 2.0" in (k / "LICENSE").read_text()[:400]
    # no CDN at runtime: the editor page references only its own files
    idx = (k / "index.html").read_text()
    assert not re.search(r"https?://", idx.replace("http://www.w3.org", ""))


def test_fixture_data_is_only_for_the_home_page_demo():
    fx = sorted(p.relative_to(WEB / "fixtures").as_posix() for p in (WEB / "fixtures").rglob("*") if p.is_file())
    assert fx == ["structures/1m17_chainA_pocket.pdb", "structures/1m17_meta.json", "structures/erlotinib_1m17.sdf"]
    users = [p.name for p in (WEB / "js").glob("*.js") if "fixtures/" in p.read_text()]
    assert users == ["home.js"]
    for p in WEB.glob("*.html"):
        assert "fixtures/" not in p.read_text(), p.name
        assert "FIXTURE" not in p.read_text(), p.name


def test_only_the_api_client_talks_to_the_network():
    """One small client (web/js/api.js). Other scripts may not call fetch/XMLHttpRequest, except the home page demo that reads its own static files."""
    allowed = {"api.js", "home.js"}
    for p in (WEB / "js").glob("*.js"):
        t = p.read_text()
        if p.name in allowed:
            continue
        assert not re.search(r"\bfetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon|EventSource", t), p.name
    api = (WEB / "js" / "api.js").read_text()
    assert 'credentials: "same-origin"' in api and 'CSRF_HEADER = "X-CSRF-Token"' in api and 'COOKIE_CSRF = "metis_csrf"' in api
    from backend.accounts.config import COOKIE_CSRF, CSRF_HEADER
    assert (CSRF_HEADER, COOKIE_CSRF) == ("X-CSRF-Token", "metis_csrf")           # the client constants mirror the backend's
    assert 'API_BASE = "/api"' in api and "http" not in re.sub(r"//.*", "", api)  # same-origin only


def test_no_dom_injection_sinks_in_own_scripts():
    banned = re.compile(r"\.innerHTML|\.outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|\.srcdoc|createContextualFragment|setTimeout\(\s*[\"'`]")
    for p in (WEB / "js").glob("*.js"):
        assert not banned.search(p.read_text()), p.name


def test_legal_copies_rendered_with_draft_note_and_disclaimer_matches_docs():
    for slug, src in [("terms", "TERMS_OF_SERVICE"), ("privacy", "PRIVACY_POLICY"), ("disclaimer", "DISCLAIMER")]:
        page = (WEB / "legal" / f"{slug}.html").read_text()
        md = (ROOT / "docs" / "legal" / f"{src}.md").read_text()
        assert "DRAFT" in page
        first = [l for l in md.splitlines() if l.strip() and not l.startswith("#")][0]
        key = re.sub(r"^[-*0-9. ]+|\*\*", "", first)[:40]
        assert key.split("**")[0][:25] in page.replace("<strong>", "").replace("</strong>", "")
    md = (ROOT / "docs" / "legal" / "DISCLAIMER.md").read_text()
    body = " ".join(l for l in md.splitlines() if l.strip() and not l.startswith("#"))
    assert json.dumps(body) in (WEB / "js" / "legal_text.js").read_text()


def _lum(hexc):
    h = hexc.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a, b):
    la, lb = sorted([_lum(a), _lum(b)], reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_contrast_of_design_tokens():
    css = (WEB / "css" / "app.css").read_text()
    blocks = re.findall(r"(:root[^{]*)\{([^}]*)\}", css)
    themes = []
    for sel, body in blocks:
        toks = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", body))
        if "bg" in toks:
            themes.append(toks)
    assert len(themes) >= 2
    for t in themes:
        for fg, bg in [("fg", "bg"), ("fg", "panel"), ("muted", "bg"), ("muted", "panel"), ("muted", "soft"), ("accent", "panel"),
                       ("on-accent", "accent"), ("err", "panel"), ("fg", "accent-soft")]:
            assert _ratio(t[fg], t[bg]) >= 4.5, (fg, bg, _ratio(t[fg], t[bg]))
    assert _ratio("#e6e9ec", "#14181c") >= 4.5 and _ratio("#0b1520", "#6aa5e6") >= 4.5 and _ratio("#a3adb8", "#14181c") >= 4.5
