"""Metis Lead Studio (Beta), web/lead/: Playwright tests against the Metis MockApi (session) and LeadMockApi (the /lead routes).

The backend for /lead is built on a parallel branch; LeadMockApi in mockapi.py is a copy of its route contract, so these
run without it. The page is served with the Content-Security-Policy that web/_headers names for /lead/index.html (the plain
static server sends no headers), so a CSP violation shows up as a console error here.
"""
import re
from pathlib import Path

import pytest
from mockapi import HOSTILE, LeadMockApi, MockApi, install_lead

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
LEAD = WEB / "lead"
NOISE = ("GL Driver", "GPU stall", "WebGL", "swiftshader", "SwiftShader")   # headless software-GL chatter, not page errors


def _csp():
    lines = (WEB / "_headers").read_text().splitlines()
    i = lines.index("/lead/index.html")
    return lines[i + 1].split("Content-Security-Policy:", 1)[1].strip()


CSP = _csp()


@pytest.fixture()
def lead_page(make_page):
    def _make(width=1440, height=900, lead=None, api="default", csp=True, **kw):
        pg = make_page(width, height, api=api, **kw)
        pg.lead = lead or LeadMockApi()
        install_lead(pg, pg.lead)
        if csp:
            def with_csp(route):
                resp = route.fetch()
                route.fulfill(response=resp, headers={**resp.headers, "content-security-policy": CSP})
            pg.route(re.compile(r".*/lead/index\.html.*"), with_csp)
        pg.requests, pg.headers_seen = [], []
        pg.on("request", lambda r: (pg.requests.append(r.url), pg.headers_seen.append((r.method, r.url, r.headers))))
        return pg
    return _make


def real_errors(pg):
    return [e for e in pg.errors if not any(n in e for n in NOISE)]


def wait_for(pg, js, timeout=30):
    """Poll a JS expression with evaluate(): wait_for_function() evals its predicate in the page, which the page's CSP (rightly) forbids."""
    for _ in range(int(timeout * 10)):
        if pg.evaluate(js):
            return
        pg.wait_for_timeout(100)
    raise AssertionError(f"timed out waiting for: {js}")


def open_lead(pg, qs=""):
    pg.goto(f"{pg.base}/lead/index.html{qs}")
    wait_for(pg, "document.querySelectorAll('#sel-target option').length > 0 && !document.getElementById('target-body').hidden", 20)


def wait_done(pg):
    wait_for(pg, "!!window.metis && window.metis.store.status === 'done'")


def pharmacology(pg):
    pg.keyboard.press("e")
    wait_for(pg, "document.body.dataset.stage === 'pharmacology'", 10)
    pg.wait_for_selector("#smiles", state="visible", timeout=10000)


# ---------------- page ----------------
def test_lead_page_loads_with_title_beta_label_and_note(lead_page):
    pg = lead_page()
    open_lead(pg)
    assert pg.title() == "Metis Lead Studio (Beta)"
    assert pg.locator("h1:visible").count() == 1 and "Metis Lead Studio" in pg.inner_text("h1")
    assert pg.inner_text("[data-testid=beta]").strip() == "Beta"
    assert pg.locator("[data-testid=beta-note]").count() == 0  # the disclaimer banner was removed on the owner's instruction (BACKLOG)
    assert pg.get_attribute("#back-metis", "href") == "../app.html" and "Back to Metis projects" in pg.inner_text("#back-metis")
    assert pg.locator(".logo").count() == 1 and "benzene.svg" in pg.evaluate("getComputedStyle(document.querySelector('.logo')).webkitMaskImage")
    assert pg.evaluate("document.fonts.check('500 14px \"IBM Plex Sans\"')")
    assert "fixture1" in pg.eval_on_selector_all("#sel-target option", "o => o.map(x => x.value)")
    assert pg.locator("#stage canvas").count() >= 1
    assert real_errors(pg) == []
    # the studio has no sign-in or account UI of its own
    assert pg.locator("text=/Sign in|Sign up|Log in|Sign out/").count() == 0
    assert ("GET", "/api/health") in [(m, p) for m, p, _ in pg.lead.calls]


def test_back_link_returns_to_metis_projects(lead_page):
    pg = lead_page()
    open_lead(pg)
    pg.click("#back-metis")
    pg.wait_for_url("**/app.html")


def test_app_nav_links_to_lead_studio(make_page):
    pg = make_page(1280, 800)
    pg.goto(pg.base + "/app.html")
    link = pg.locator("nav.nav a", has_text="Lead Studio (Beta)")
    assert link.count() == 1 and link.get_attribute("href") == "lead/index.html"
    pg.goto(pg.base + "/index.html")                                  # the home page keeps its wireframe: no extra button there
    assert pg.locator("body a:visible").count() == 2


def test_no_external_requests_and_no_console_errors_across_stages(lead_page):
    pg = lead_page()
    open_lead(pg)
    for key in "wrqe":
        pg.keyboard.press(key)
        pg.wait_for_timeout(150)
    pg.click("#btn-help")
    pg.keyboard.press("Escape")
    outside = [u for u in pg.requests if not u.startswith((pg.base, "data:", "blob:"))]
    assert outside == []
    assert real_errors(pg) == []


# ---------------- identity: the Metis session ----------------
def test_signed_out_visitor_is_sent_to_metis_sign_in_and_back(make_page):
    pg = make_page(api=MockApi(signed_in=False))
    install_lead(pg, LeadMockApi())
    pg.goto(pg.base + "/lead/index.html")
    pg.wait_for_url("**/auth.html?*")
    assert pg.url.startswith(pg.base + "/auth.html") and "next=lead%2Findex.html" in pg.url
    assert "Sign in to continue" in pg.inner_text("#auth-banner")


def test_a_401_from_the_lead_api_also_leaves_for_sign_in(lead_page):
    pg = lead_page()
    pg.lead.status_overrides[("GET", r"^/api/targets$")] = (401, {"detail": "not_authenticated"})
    pg.goto(pg.base + "/lead/index.html")
    pg.wait_for_url("**/auth.html?*")
    assert "next=lead%2Findex.html" in pg.url


def test_terms_reacceptance_is_handled_like_the_rest_of_metis(lead_page):
    pg = lead_page()
    pg.lead.status_overrides[("GET", r"^/api/targets$")] = (403, {"detail": "terms_reacceptance_required"})
    pg.goto(pg.base + "/lead/index.html")
    pg.wait_for_url("**/auth.html?*")
    assert "why=terms" in pg.url


def test_lead_calls_use_the_metis_csrf_header_and_same_origin_lead_paths(lead_page):
    pg = lead_page()
    open_lead(pg)
    pg.click("#btn-stage-run")
    wait_done(pg)
    posts = [(u, h) for m, u, h in pg.headers_seen if m == "POST"]
    assert posts, "no POST seen"
    for u, h in posts:
        assert re.search(r"/lead/(api|library|profiles)/", u), u
        assert h.get("x-csrf-token") == "testtoken" and "x-requested-with" not in h
    lead_urls = [u for u in pg.requests if "/lead/" in u and not u.endswith((".js", ".css", ".html", ".svg", ".woff2"))]
    assert lead_urls and all(re.search(r"/lead/(api|library|profiles)/", u) for u in lead_urls)
    assert not [u for u in pg.requests if re.search(r"/api/(runs|targets|molecules|poses|manual)", u) and "/lead/api/" not in u]


# ---------------- a run ----------------
def test_run_is_created_and_streamed_to_completion(lead_page):
    pg = lead_page()
    open_lead(pg)
    assert pg.is_enabled("#btn-stage-run")
    pg.click("#btn-stage-run")
    wait_done(pg)
    created = [b for m, p, b in pg.lead.calls if (m, p) == ("POST", "/api/runs")]
    assert len(created) == 1 and created[0]["stage"] == "binding" and created[0]["target_id"] == "fixture1" and created[0]["seed_smiles"]
    n = pg.evaluate("window.metis.store.candidates.length")
    assert n == sum(e["type"] == "candidate" for e in pg.lead.events) and n > 10
    assert pg.evaluate("window.metis.store.generations.length") == 3
    pg.wait_for_selector("#leads-table tr.lead")
    assert pg.locator("#leads-table tr.lead").count() >= 2
    assert pg.is_enabled("#btn-stage-run")
    assert "PREDICTED" in pg.inner_text("#card-leads").upper()
    assert real_errors(pg) == []


def test_unreachable_backend_is_said_plainly_not_faked(lead_page):
    pg = lead_page(lead=LeadMockApi(fail_health=True))
    pg.goto(pg.base + "/lead/index.html")
    pg.wait_for_selector("#offline-banner:not([hidden])")
    assert "offline" in pg.inner_text("#offline-banner").lower() and "recorded or simulated" in pg.inner_text("#offline-banner")
    assert pg.get_attribute("#btn-stage-run", "disabled") is not None
    assert pg.evaluate("window.metis.store.candidates.length") == 0


def test_validate_shows_server_error_as_text(lead_page):
    pg = lead_page()
    open_lead(pg)
    pharmacology(pg)
    pg.fill("#smiles", "not a smiles (((")
    pg.click("#btn-validate")
    wait_for(pg, "document.getElementById('validate-msg').textContent.includes('Could not parse')", 10)
    pg.fill("#smiles", "CCO")
    pg.click("#btn-validate")
    pg.wait_for_selector("#valid-info:not([hidden])")
    assert "Canonical SMILES" in pg.inner_text("#valid-info") and "CCO" in pg.inner_text("#valid-info")


# ---------------- hostile strings ----------------
def test_hostile_strings_never_become_dom(lead_page):
    lead = LeadMockApi(hostile=True)
    pg = lead_page(lead=lead)
    dialogs = []
    pg.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    open_lead(pg)
    pharmacology(pg)
    # a hostile SMILES typed by the user, echoed back by the server (and the server's own hostile canonical SMILES)
    pg.fill("#smiles", "CCO")
    pg.click("#btn-validate")
    pg.wait_for_selector("#valid-info:not([hidden])")
    pg.fill("#smiles", "<img src=x onerror=window.__pwn=1>")
    pg.click("#btn-validate")
    wait_for(pg, "document.getElementById('validate-msg').textContent.includes('Could not parse')", 10)
    # a hostile run (edit names, rationales, tags, alert names and families, SMILES, headline)
    pg.keyboard.press("q")
    wait_for(pg, "document.body.dataset.stage === 'binding'", 10)
    pg.click("#btn-stage-run")
    wait_done(pg)
    # manual mode: the dock names the seed molecule and the logging state
    pg.keyboard.press("r")
    pg.wait_for_selector("[data-act=start-seed]", timeout=10000)
    pg.click("[data-act=start-seed]")
    pg.wait_for_selector("[data-act=undo]", timeout=10000)
    # walk the stages, the timeline (the "why" card shows edit names / rationales / tags / alerts), the library drawer
    pg.click("#tl-next")
    pg.click("#tl-next")
    for key in "wqer":
        pg.keyboard.press(key)
        pg.wait_for_timeout(120)
    pg.click("text=My library")
    pg.wait_for_selector(".acct-drawer")
    pg.wait_for_timeout(300)
    assert pg.evaluate("window.__pwn") is None and dialogs == []
    assert pg.locator("#pwn, [id^=pwn], img[src='x']").count() == 0
    assert pg.evaluate("[...document.querySelectorAll('img')].filter(i => !i.closest('#stage')).length") == 0
    # the strings are on the page, as text, in the places that render server strings
    body = pg.inner_text("body")
    assert "<img src=x" in body
    shown = pg.evaluate("""['#target-meta', '#seed-smiles', '#sel-target', '#sel-drug', '#leads-table', '#why-body', '#hud-title', '#validate-msg', '#valid-info', '.acct-drawer', '#panel-story', '#panel-lineage', '#panel-charts', '#panel-map', '#panel-manual', '#card-pose']
        .filter(s => (document.querySelector(s)?.textContent || '').includes('<img src=x') || [...document.querySelectorAll(s + ' option')].some(o => o.textContent.includes('<img src=x'))) """)
    print("hostile text shown in", shown)
    assert {"#seed-smiles", "#leads-table", "#why-body", "#hud-title", "#validate-msg"} <= set(shown)
    assert real_errors(pg) == []


def test_library_lists_use_text_not_markup(lead_page):
    lead = LeadMockApi()
    lead.library = [{"id": "lib-1", "title": HOSTILE + "title", "stage": "binding", "target_id": HOSTILE, "headline": HOSTILE, "created_at": "2026-01-01T00:00:00Z"}]
    pg = lead_page(lead=lead)
    open_lead(pg)
    pg.click("text=My library")
    pg.wait_for_selector(".acct-item")
    assert pg.locator("#pwn, img[src='x']").count() == 0 and "<img src=x" in pg.inner_text(".acct-item")
    assert pg.evaluate("window.__pwn") is None
    assert ("GET", "/library/runs") in [(m, p) for m, p, _ in pg.lead.calls]


# ---------------- layout and keyboard ----------------
def test_phone_width_has_no_horizontal_scroll(lead_page):
    pg = lead_page(390, 844)
    open_lead(pg)
    pg.click("#btn-stage-run")
    wait_done(pg)
    pg.wait_for_timeout(600)
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390
    assert pg.evaluate("document.body.scrollWidth") <= 390


def test_controls_are_keyboard_reachable_and_labelled(lead_page):
    pg = lead_page()
    open_lead(pg)
    pharmacology(pg)
    pg.click("body", position={"x": 5, "y": 5})
    want = {"back-metis", "btn-help", "btn-export", "btn-theme", "sel-target", "sel-drug", "btn-stage-run", "smiles", "btn-validate", "tl-play"}
    seen = set()
    for _ in range(90):
        pg.keyboard.press("Tab")
        ident = pg.evaluate("document.activeElement && (document.activeElement.id || '')")
        if ident:
            seen.add(ident)
        if want <= seen:
            break
    assert want <= seen, want - seen
    # every field and button has an accessible name
    unlabeled = pg.evaluate("""[...document.querySelectorAll('input:not([type=hidden]),select,textarea')].filter(e => {
        if (e.getAttribute('aria-label') || e.getAttribute('aria-labelledby') || e.closest('label')) return false;
        return !(e.id && document.querySelector('label[for="' + e.id + '"]'));}).map(e => e.id || e.name)""")
    assert unlabeled == []
    assert pg.evaluate("[...document.querySelectorAll('button')].filter(b => !b.textContent.trim() && !b.getAttribute('aria-label') && !b.title).length") == 0
    # Enter on the focused Run button starts the run
    pg.focus("#btn-stage-run")
    pg.keyboard.press("Enter")
    wait_done(pg)


# ---------------- static rules for web/lead ----------------
LEAD_FILES = [p for p in LEAD.rglob("*") if p.is_file()]


def test_lead_html_has_no_inline_script_handler_or_style():
    html = (LEAD / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script>"
    assert not re.search(r"\son[a-z]+\s*=", html) and not re.search(r"\sstyle\s*=", html) and "<style" not in html and "javascript:" not in html
    assert "cdnjs" not in html and not re.search(r"https?://", html)
    assert "<title>Metis Lead Studio (Beta)</title>" in html


def test_lead_scripts_have_no_style_attributes_storage_or_stray_network_calls():
    for p in LEAD.rglob("*.js"):
        t = re.sub(r"//[^\n]*", "", p.read_text())
        assert not re.search(r"\sstyle=\"|setAttribute\(\s*[\"']style|cssText|localStorage|sessionStorage|indexedDB|document\.write|\beval\(|new Function|document\.cookie", t), p.name
        assert not re.search(r"\bfetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket|importScripts", t), p.name
        if p.name != "api.js":
            assert "EventSource(" not in t, p.name
        assert not re.search(r"X-Requested-With", t), p.name
    # every Lead call goes under /lead through the Metis client
    net = (LEAD / "js" / "net.js").read_text()
    assert 'BASE = "/api/lead"' in net and "../../js/api.js" in net
    for p in LEAD.rglob("*.js"):
        for m in re.finditer(r"\blead\(\s*\"[A-Z]+\"\s*,\s*[\"'`]([^\"'`]*)", p.read_text()):
            assert m.group(1).startswith(("/api/", "/library", "/profiles")), (p.name, m.group(1))


def test_lead_reuses_shared_vendored_files_instead_of_copies():
    names = {p.name for p in LEAD_FILES}
    assert not (names & {"3Dmol-min.js", "plex-sans-400.woff2", "plex-sans-500.woff2", "plex-sans-600.woff2", "benzene.svg"})
    assert "../vendor/3Dmol-min.js" in (LEAD / "index.html").read_text()


def test_headers_file_has_a_lead_entry_no_looser_than_the_metis_pages():
    h = (WEB / "_headers").read_text().splitlines()
    metis = h[h.index("/index.html") + 1]
    for path in ("/lead/", "/lead/index.html", "/lead/js/*", "/lead/css/*"):
        assert h[h.index(path) + 1] == metis, path
    assert "'unsafe-inline'" not in CSP and "'unsafe-eval'" not in CSP and "connect-src 'self'" in CSP


def test_pose_metrics_hud_is_only_visible_on_the_pose_stage(lead_page):
    pg = lead_page()
    open_lead(pg)
    for stage, shown in (("binding", False), ("pharmacology", False), ("pose", True)):
        pg.evaluate("s => { document.body.dataset.stage = s; const h = document.getElementById('hud-pose'); h.hidden = false; h.textContent = 'Pose confidence 0.66' }", stage)
        assert pg.evaluate("getComputedStyle(document.getElementById('hud-pose')).display !== 'none'") is shown, stage


# ---------------- Wave 4: pose wait, leftovers on other stages, pixel ratio ----------------
def run_pose(pg):
    pg.keyboard.press("w")
    wait_for(pg, "document.body.dataset.stage === 'pose'", 10)
    pg.click("#btn-stage-run")


def test_pose_wait_shows_one_cartoon_and_elapsed_seconds(lead_page):
    pg = lead_page(lead=LeadMockApi(pose_polls=6))
    open_lead(pg)
    run_pose(pg)
    pg.wait_for_selector("[data-testid=pose-wait-label]", timeout=15000)
    # one plain cartoon on the protein, no sticks, no surface, a single colour
    shape = pg.evaluate("""() => { const m = window.metis.pocket.main; const at = m.model.selectedAtoms({});
        return { kinds: [...new Set(at.map(a => Object.keys(a.style || {}).sort().join('+')))], colors: [...new Set(at.map(a => a.style.cartoon && a.style.cartoon.color))].length, surfaces: m.surfaces.length }; }""")
    assert shape == {"kinds": ["cartoon"], "colors": 1, "surfaces": 0}, shape
    # software GL can stall the page for seconds, so record every label the wait shows instead of sampling at fixed times
    pg.evaluate("""() => { window.__labels = []; new MutationObserver(() => { const t = document.querySelector('[data-testid=pose-wait-label]'); if (t) window.__labels.push(t.textContent); })
        .observe(document.getElementById('hud-progress'), { childList: true, subtree: true, characterData: true }); }""")
    wait_for(pg, "!!window.metis.poseView.result", 60)
    secs = [int(m.group(1)) for t in pg.evaluate("window.__labels") if (m := re.search(r"\((\d+) s\)", t))]
    assert len(set(secs)) >= 2 and secs == sorted(secs), secs      # the count of elapsed seconds is shown and moves forward
    assert real_errors(pg) == []


def test_pose_wait_is_reachable_and_startable_from_the_keyboard(lead_page):
    pg = lead_page()
    open_lead(pg)
    pg.keyboard.press("w")
    wait_for(pg, "document.body.dataset.stage === 'pose'", 10)
    pg.focus("#btn-stage-run")
    pg.keyboard.press("Enter")
    wait_for(pg, "!!window.metis.poseView.result", 30)
    assert any(c[0] == "POST" and c[1] == "/api/poses" for c in pg.lead.calls)


def test_pose_card_and_3d_marks_do_not_stay_on_other_stages(lead_page):
    pg = lead_page()
    open_lead(pg)
    run_pose(pg)
    wait_for(pg, "!!window.metis.poseView.result && window.metis.poseView.main", 30)
    # force a displacement mark set so there is something to leak
    pg.evaluate("""() => { const v = window.metis.poseView; const a = v.main.atoms.filter(x => x.name === 'CA').slice(0, 3);
        v.moved = a.map((x, i) => ({ d: 2 + i, resi: x.resi, resn: x.resn, apo: x, pred: { x: x.x + 1, y: x.y, z: x.z } })); v._marks(); }""")
    assert pg.evaluate("window.metis.poseView.shapes.length > 0 && window.metis.poseView.labels.length > 0")
    assert pg.evaluate("getComputedStyle(document.getElementById('card-pose')).display") != "none"
    for key, stage in (("q", "binding"), ("e", "pharmacology"), ("r", "manual")):
        pg.keyboard.press("w")
        wait_for(pg, "document.body.dataset.stage === 'pose'", 10)
        pg.evaluate("() => { const v = window.metis.poseView; if (v.main) { v.moved = v.moved || []; } }")
        pg.keyboard.press(key)
        wait_for(pg, f"document.body.dataset.stage === '{stage}'", 10)
        pg.wait_for_timeout(400)
        left = pg.evaluate("""() => { const v = window.metis.poseView; return { active: v.active, shapes: v.shapes.length, labels: v.labels.length, layers: v.layers.length, lig: !!v.lig, orbit: !!v.orbit,
            card: getComputedStyle(document.getElementById('card-pose')).display, hud: getComputedStyle(document.getElementById('hud-pose')).display,
            prog: getComputedStyle(document.getElementById('hud-progress')).display }; }""")
        assert left == {"active": False, "shapes": 0, "labels": 0, "layers": 0, "lig": False, "orbit": False, "card": "none", "hud": "none", "prog": "none"}, (stage, left)
        shapes = pg.evaluate("window.metis.stage.viewer.getInternalState ? (window.metis.stage.viewer.getInternalState().shapes || []).length : 0")
        assert shapes == 0, (stage, shapes)


def test_leaving_the_pose_stage_during_the_wait_stops_the_waiting_ligand(lead_page):
    pg = lead_page(lead=LeadMockApi(pose_polls=50))
    open_lead(pg)
    run_pose(pg)
    pg.wait_for_selector("[data-testid=pose-wait-label]", timeout=15000)
    pg.keyboard.press("q")
    wait_for(pg, "document.body.dataset.stage === 'binding'", 10)
    pg.wait_for_timeout(500)
    assert pg.evaluate("({ orbit: !!window.metis.poseView.orbit, tick: !!window.metis.poseView._tickTimer })") == {"orbit": False, "tick": False}


def test_lead_viewer_pixel_ratio_is_capped_at_1_5(lead_page):
    pg = lead_page(device_scale_factor=3)
    open_lead(pg)
    assert pg.evaluate("window.devicePixelRatio") == 1.5
    r = pg.evaluate("""() => { const c = document.querySelector('#stage canvas, canvas'); const cs = [...document.querySelectorAll('canvas')].filter(x => x.clientWidth > 100);
        return cs.map(x => x.width / x.clientWidth); }""")
    assert r and all(x <= 1.51 for x in r), r
    pg2 = lead_page(device_scale_factor=1)
    open_lead(pg2)
    assert pg2.evaluate("window.devicePixelRatio") == 1
