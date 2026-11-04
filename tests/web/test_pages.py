"""Behaviour of the static screens in headless Chromium (Playwright)."""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGES = ["index.html", "auth.html", "app.html", "add.html", "compound.html?c=3&e=herg", "model.html?e=herg", "docking.html", "account.html"]
DISCLAIMER = " ".join(l for l in (ROOT / "docs/legal/DISCLAIMER.md").read_text().splitlines() if l.strip() and not l.startswith("#"))
NO_WEBGL = "const g=HTMLCanvasElement.prototype.getContext;HTMLCanvasElement.prototype.getContext=function(t,...a){if(/webgl/.test(t))return null;return g.call(this,t,...a)};"


def center(box):
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


# ---------------- home ----------------
def test_home_layout_1440x900(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/index.html")
    pg.wait_for_selector("#mol3d canvas")
    logo, word, mol, cta = (pg.locator(s).bounding_box() for s in (".logo", ".word", "#mol3d", "#cta"))
    assert logo["x"] < 60 and logo["y"] < 60 and logo["width"] < 100                    # top-left
    wx, wy = center(word)
    assert wx < 720 and abs(wy - 450) < 20                                              # left of center, vertically centered
    mx, my = center(mol)
    assert mx > 720 and my < 450 and mol["width"] >= 450 and mol["x"] + mol["width"] <= 1440   # large, top-right
    cx, cy = center(cta)
    assert abs(cx - 720) < 2 and cy > 800                                               # bottom-center
    assert word["x"] + word["width"] < mol["x"]                                         # no overlap
    # nothing else on the page
    visible = pg.evaluate("[...document.body.querySelectorAll('*')].filter(e=>e.getBoundingClientRect().width>0 && !e.closest('#mol3d') && !['SCRIPT','STYLE'].includes(e.tagName)).map(e=>e.tagName)")
    assert sorted(visible) == sorted(["A", "IMG", "MAIN", "H1", "A"])
    assert pg.locator("#cta").inner_text().strip() == "Sign up / Log in"
    assert pg.locator("h1").inner_text() == "Metis"
    assert "IBM Plex Sans" in pg.evaluate("getComputedStyle(document.querySelector('.word')).fontFamily")
    assert pg.evaluate("document.fonts.check('500 20px \"IBM Plex Sans\"')")
    assert pg.locator(".logo img").get_attribute("src") == "assets/benzene.svg"
    assert not pg.errors or all("GL Driver" in e for e in pg.errors)


def test_home_always_dark_even_with_light_scheme(make_page):
    pg = make_page(1440, 900, color_scheme="light")
    pg.goto(pg.base + "/index.html")
    assert pg.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(20, 24, 28)"


def test_home_spins_and_pauses_when_hidden(make_page):
    pg = make_page(800, 600)
    pg.goto(pg.base + "/index.html")
    pg.wait_for_selector("#mol3d[data-spin='on']")
    pg.wait_for_function("+document.getElementById('mol3d').dataset.frames >= 2", timeout=20000)
    pg.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true}); document.dispatchEvent(new Event('visibilitychange'))")
    assert pg.get_attribute("#mol3d", "data-spin") == "off"
    pg.wait_for_timeout(300)
    f1 = pg.get_attribute("#mol3d", "data-frames")
    pg.wait_for_timeout(1500)
    assert pg.get_attribute("#mol3d", "data-frames") == f1
    pg.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>false}); document.dispatchEvent(new Event('visibilitychange'))")
    pg.wait_for_selector("#mol3d[data-spin='on']")


def test_home_no_spin_under_reduced_motion(make_page):
    pg = make_page(800, 600, reduced_motion="reduce")
    pg.goto(pg.base + "/index.html")
    pg.wait_for_selector("#mol3d canvas")
    pg.wait_for_timeout(1500)
    assert pg.get_attribute("#mol3d", "data-spin") == "off"
    assert pg.get_attribute("#mol3d", "data-frames") == "0"


def test_home_webgl_fallback_shows_2d(make_page):
    pg = make_page(800, 600)
    pg.add_init_script(NO_WEBGL)
    pg.goto(pg.base + "/index.html")
    pg.wait_for_selector("#mol3d .fallback img")
    assert pg.get_attribute("#mol3d", "data-webgl") == "off"
    assert "WebGL" in pg.inner_text("#mol3d")


# ---------------- generic: mobile, a11y ----------------
@pytest.mark.parametrize("page_url", PAGES)
def test_no_horizontal_scroll_at_390(make_page, page_url):
    pg = make_page(390, 844)
    pg.goto(pg.base + "/" + page_url)
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(500)
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390
    assert pg.evaluate("document.body.scrollWidth") <= 390


@pytest.mark.parametrize("page_url", PAGES[1:])
def test_basic_accessibility(make_page, page_url):
    pg = make_page(1280, 800)
    pg.goto(pg.base + "/" + page_url)
    pg.wait_for_load_state("networkidle")
    assert pg.evaluate("document.documentElement.lang") == "en"
    assert pg.title().strip() != ""
    assert pg.locator("h1").count() == 1
    assert pg.evaluate("[...document.images].filter(i=>!i.hasAttribute('alt')).length") == 0
    unlabeled = pg.evaluate("""[...document.querySelectorAll('input:not([type=hidden]),select,textarea')].filter(e=>{
        if(e.getAttribute('aria-label')||e.getAttribute('aria-labelledby')) return false;
        if(e.closest('label')) return false; return !(e.id&&document.querySelector('label[for="'+e.id+'"]'));}).map(e=>e.id||e.name)""")
    assert unlabeled == []
    assert pg.evaluate("[...document.querySelectorAll('button')].filter(b=>!b.textContent.trim()&&!b.getAttribute('aria-label')).length") == 0
    assert pg.locator(".skip").count() == 1 or page_url.startswith("auth")
    assert pg.evaluate("document.querySelector('main')!==null")


@pytest.mark.parametrize("page_url", ["app.html", "add.html", "compound.html?c=3&e=herg", "model.html?e=herg", "docking.html", "account.html"])
def test_fixture_label_and_disclaimer_on_screens(make_page, page_url):
    pg = make_page(1280, 800)
    pg.goto(pg.base + "/" + page_url)
    assert "FIXTURE" in pg.inner_text("[data-testid=fixture-banner]")
    assert pg.inner_text("[data-testid=disclaimer]").strip() == DISCLAIMER


# ---------------- sign up ----------------
def test_signup_requires_unchecked_by_default_acceptance(make_page):
    pg = make_page(1280, 800)
    pg.goto(pg.base + "/auth.html")
    cb, btn = pg.locator("#accept"), pg.locator("#signup-submit")
    assert not cb.is_checked() and btn.is_disabled()
    assert "DRAFT" in pg.inner_text("[data-testid=draft-note]")
    cb.check()
    assert btn.is_enabled()
    cb.uncheck()
    assert btn.is_disabled()
    label = pg.inner_text("#accept-label")
    for w in ("Terms of Service", "Privacy Policy", "Disclaimer"):
        assert w in label
    hrefs = pg.eval_on_selector_all("#accept-links a", "els=>els.map(e=>e.getAttribute('href'))")
    assert hrefs == ["legal/terms.html", "legal/privacy.html", "legal/disclaimer.html"]
    for h in hrefs:
        pg2 = pg.context.new_page()
        resp = pg2.goto(pg.base + "/" + h)
        assert resp.ok and "DRAFT" in pg2.inner_text("body")
        pg2.close()
    # keyboard: space toggles the checkbox
    cb.focus(); pg.keyboard.press("Space")
    assert btn.is_enabled()
    pg.click("#tab-login")
    assert pg.locator("#login-submit").is_enabled()


# ---------------- project table ----------------
def test_prediction_cells_complete_and_neutral(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/app.html")
    pg.wait_for_selector("tr.sel-row")
    cells = pg.locator("[data-pred]")
    n = cells.count()
    assert n == 8 * 5
    for i in range(n):
        c = cells.nth(i)
        t = c.inner_text()
        kind = c.get_attribute("data-pred")
        assert re.search(r"N=\d+", t) and re.search(r"n=[\d,]+", t), t
        if kind in ("herg", "ames"):
            assert re.search(r"\d\.\d\d \(cal\.\)", t) and "calibrated probability" in t
        else:
            assert re.search(r"-?\d+\.\d \[-?\d+\.\d, -?\d+\.\d\]", t) and re.search(r"\d+% interval", t), t
        assert c.get_attribute("data-coverage") and c.get_attribute("data-n") and c.get_attribute("data-sample-size")
        # no status colour classes or verdict words inside the cell
        html = c.inner_html()
        assert not re.search(r'class="[^"]*\b(ok|warn|bad|good|fail|pass|success|danger)\b', html)
        assert not re.search(r"\b(safe|unsafe|toxic|good|bad|promising|recommended)\b", t, re.I)
    # no verdict column
    heads = pg.eval_on_selector_all("#thead th", "els=>els.map(e=>e.textContent)")
    assert not any(re.search("verdict|status|flag", h, re.I) for h in heads)
    # column headers link to model pages; values link to the uncertainty panel
    assert pg.locator("#thead a[href='model.html?e=herg']").count() == 1
    assert pg.locator("[data-pred=herg] a").first.get_attribute("href").startswith("compound.html?c=")
    # computed styles of prediction cells: no shadows / gradients
    assert pg.evaluate("[...document.querySelectorAll('[data-pred], [data-pred] *')].some(e=>{const s=getComputedStyle(e);return s.boxShadow!=='none'||s.textShadow!=='none'||s.backgroundImage!=='none'})") is False


def test_2d_everywhere_and_one_shared_3d_viewer(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/app.html")
    pg.wait_for_selector("tr.sel-row")
    assert pg.locator("tr.sel-row img.mol").count() == 8 and pg.locator("tr.sel-row button:has-text('3D')").count() == 8
    for i in (0, 3, 5):
        pg.locator("tr.sel-row button:has-text('3D')").nth(i).click()
        pg.wait_for_timeout(200)
    assert pg.locator("canvas").count() == 1          # one shared viewer, one WebGL context
    assert "computed conformer, not an experimental structure" in pg.inner_text("#viewer-cap")
    assert pg.get_attribute("#viewer", "data-webgl") == "on"


def test_webgl_missing_keeps_2d_on_every_3d_screen(make_page):
    for url in ["app.html", "compound.html?c=3&e=herg", "model.html?e=herg", "docking.html"]:
        pg = make_page(1280, 800)
        pg.add_init_script(NO_WEBGL)
        pg.goto(pg.base + "/" + url)
        pg.wait_for_selector("[data-testid=webgl-notice]")
        assert pg.locator("canvas").count() == 0
        assert "2D" in pg.inner_text("[data-testid=webgl-notice]")
        assert pg.locator("img.mol").count() >= 1, url
        assert not [e for e in pg.errors if "Cannot" in e or "undefined" in e], pg.errors


def test_ranking_panel_defaults_and_interaction(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/app.html")
    pg.wait_for_selector("tr.sel-row")
    assert pg.get_attribute("#lam", "value") == "25" and pg.inner_text("#lamv") == "0.25"
    assert pg.get_attribute("#lam", "min") == "0" and pg.get_attribute("#lam", "max") == "100"
    assert "Best properties first" in pg.inner_text("#rankpanel") and "Least uncertain first" in pg.inner_text("#rankpanel")
    assert "R_j = (1 - lambda) S_j + lambda C_j" in pg.inner_text("#formula-text")
    ranks = lambda: pg.eval_on_selector_all("[data-testid=rank]", "els=>els.map(e=>e.firstChild.textContent)")
    before = ranks()
    assert all(re.fullmatch(r"\d+ \(\d+\)", r) for r in before)
    assert pg.inner_text("[data-testid=nfail]") == "2 fail a hard limit"
    assert pg.locator(".limit-note:has-text('limit: < 0.3')").count() == 2    # compounds 3 and 4 violate hERG < 0.3
    # stacked breakdown sums to R
    shares = [float(x) for x in pg.eval_on_selector_all("#bd-table tbody tr td:last-child", "els=>els.map(e=>e.textContent)")]
    R = float(re.search(r"R = ([\d.]+)", pg.inner_text("#bd-title")).group(1))
    assert abs(sum(shares) - R) < 0.0035 and len(shares) == 5
    # lambda to 1 changes the blended rank order, property-only ranks do not move
    pg.locator("#lam").fill("100")
    after = ranks()
    po = lambda rs: sorted(r.split("(")[1] for r in rs)
    assert pg.inner_text("#lamv") == "1.00" and after != before
    pg.locator("#lam").fill("0")
    assert [r.split()[0] for r in ranks()] == [r.split("(")[1].rstrip(")") for r in ranks()]   # lambda 0: blended == property-only
    # weights editable
    pg.locator("[data-weight=herg]").fill("0")
    assert pg.inner_text("#bd-table tbody tr:nth-child(3) td:last-child") == "0.000"


# ---------------- add compounds ----------------
def test_add_compounds_cap_ensemble_time_and_consent(make_page):
    pg = make_page(1280, 800)
    pg.goto(pg.base + "/add.html")
    assert "Up to 10 compounds per submission" in pg.inner_text("[data-testid=cap-note]")
    opts = lambda: pg.eval_on_selector_all("#ens option", "els=>els.map(e=>[e.value,e.disabled,e.selected])")
    assert opts() == [["3", False, False], ["5", False, True], ["10", False, False], ["20", True, False]]
    pg.select_option("#tier", "edu")
    assert [o[1] for o in opts()] == [False] * 4
    pg.select_option("#tier", "free")
    assert opts()[3][1] is True
    assert "Estimated time: 48 s for 3 compounds and 6 properties at N = 5" in pg.inner_text("[data-testid=eta]")
    pg.fill("#smi", "\n".join(f"{'C' * (i + 1)} m{i}" for i in range(12)))
    assert pg.inner_text("[data-testid=slots]") == "10 of 10 slots used"
    assert pg.locator("#check .tag[data-kind=error]").count() == 2
    pg.fill("#smi", "CC(=O\nC1CC\nCC(=O)O")
    assert pg.locator("#check .tag[data-kind=error]").count() == 2 and pg.inner_text("[data-testid=slots]") == "1 of 10 slots used"
    assert "private by default" in pg.inner_text("[data-testid=consent-line]") and "opt in" in pg.inner_text("[data-testid=consent-line]")
    # tab keyboard nav + draw tab shows 2D and live 3D preview
    pg.focus("#t-paste"); pg.keyboard.press("ArrowRight"); pg.keyboard.press("ArrowRight")
    assert pg.get_attribute("#t-draw", "aria-selected") == "true"
    pg.wait_for_selector("#draw2d img.mol")
    assert pg.get_attribute("#viewer", "data-webgl") == "on"


# ---------------- uncertainty panel ----------------
def test_uncertainty_panel_content(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/compound.html?c=3&e=herg")
    pg.wait_for_selector("#nearest .mol")
    t = pg.inner_text("#valcard")
    assert "0.72" in t and "151 calibration compounds" in t and "74%" in t and "N = 10" in t and "n=1,240" in t
    shares = pg.inner_text("#shares")
    assert "46%" in shares and "38%" in shares and "16%" in shares and "range 38 to 55" in shares
    pcts = [int(x) for x in re.findall(r"(\d+)%", " ".join(pg.eval_on_selector_all("#shares [data-share] .mono", "e=>e.map(x=>x.textContent)")))]
    assert sum(pcts) == 100
    assert pg.locator("#d2 img.mol[src$='c3_hl.svg']").count() == 1                 # 2D with highlighted fragment
    assert "sulfonamide: 38%" in pg.inner_text("#d2cap")
    assert pg.get_attribute("#viewer", "data-webgl") == "on" and "highlighted fragment: sulfonamide" in pg.inner_text("#viewer-cap")
    assert "12 of 8,400 training compounds" in pg.inner_text("#fragtext")
    assert pg.locator("#nearest [data-nearest]").count() == 3
    pg.locator("#nearest button[aria-label*='nearest']").first.click()
    assert "Training compound" in pg.inner_text("#viewer-cap")
    assert "describes model behavior, not chemistry" in pg.inner_text("main")
    assert pg.locator("a:has-text('How these numbers were computed')").count() == 1
    # regression endpoint uses the same layout
    pg.goto(pg.base + "/compound.html?c=1&e=solubility")
    assert re.search(r"-3\.1 \[-3\.9, -2\.4\]", pg.inner_text("[data-testid=panel-value]"))
    assert "90% interval" in pg.inner_text("#valcard")


# ---------------- model page ----------------
def test_model_page_fields(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/model.html?e=herg")
    pg.wait_for_selector("#report table")
    t = pg.inner_text("#report")
    assert "hERG_Karim" in t and "pending" in t and "Scaffold" in t and "Known limitations" in t
    first = pg.locator("#report table").first.locator("tbody tr").first.inner_text()
    assert "baseline" in first
    ens = pg.locator("#report table").nth(1).locator("tbody tr")
    assert ens.count() == 4 and "placeholder" in t
    assert pg.locator("#examples img.mol").count() == 3
    assert "FIXTURE" in pg.inner_text("[data-testid=fixture-model-note]")


# ---------------- docking ----------------
def test_docking_prep(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/docking.html")
    pg.wait_for_selector("#sites tbody tr")
    assert pg.get_attribute("#viewer", "data-webgl") == "on" and pg.locator("#lig2d").count() == 1
    assert "Does not run docking" in pg.inner_text("main")
    assert "PDBQT" in pg.inner_text("#eng-note")
    pg.check("input[name=eng][value=gnina]")
    assert "GNINA" in pg.inner_text("#eng-note") and "SDF" in pg.inner_text("#eng-note")
    assert pg.locator("#sites tbody tr").count() == 3 and "Change log" in pg.inner_text("main")
    with pg.expect_download() as dl:
        pg.click("#dl-box")
    assert dl.value.suggested_filename == "gnina_box.txt"


# ---------------- account ----------------
def test_account_consents_default_off_and_delete(make_page):
    pg = make_page(1280, 800)
    pg.goto(pg.base + "/account.html")
    boxes = pg.locator("[data-consent]")
    assert boxes.count() == 3
    assert all(not boxes.nth(i).is_checked() for i in range(3))
    assert "30 days" in pg.inner_text("main") and "Delete all my data" in pg.inner_text("main")
    pg.click("#del-all")
    assert pg.locator("#c-ok").is_disabled()
    pg.fill("#c-in", "DELETE")
    assert pg.locator("#c-ok").is_enabled()
    pg.click("#c-ok")
    pg.wait_for_function("document.getElementById('del-msg').textContent.includes('nothing is stored')")
