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


def test_fixtures_labeled_and_match_frozen_contract():
    from backend.schemas.contracts import Prediction, MAX_COMPOUNDS
    proj = json.loads((WEB / "fixtures" / "project.json").read_text())
    assert proj["fixture"] is True and "FIXTURE" in proj["fixture_label"]
    assert len(proj["compounds"]) <= MAX_COMPOUNDS
    keep = ["endpoint", "kind", "model_version_id", "ensemble_n", "value", "interval_low", "interval_high", "probability",
            "coverage", "sample_size", "method_note", "breakdown"]
    for c in proj["compounds"]:
        assert c["conformer"]["label"] == "computed conformer, not an experimental structure"
        assert "ETKDG" in c["conformer"]["method"] and "MMFF" in c["conformer"]["method"]
        assert (WEB / c["svg"]).exists() and (WEB / c["svg_highlight"]).exists()
        assert max(c["fragment"]["atom_indices"]) < c["conformer"]["molblock"].count("\n")
        for ep, p in c["predictions"].items():
            d = {k: p[k] for k in keep}
            d["breakdown"] = {k: v for k, v in p["breakdown"].items() if k != "ranges"}
            Prediction(**d)
            for lo, hi in p["breakdown"]["ranges"]:
                assert 0 <= lo <= hi <= 1
    for name in ["models", "docking", "training"]:
        assert json.loads((WEB / "fixtures" / f"{name}.json").read_text())["fixture"] is True


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
