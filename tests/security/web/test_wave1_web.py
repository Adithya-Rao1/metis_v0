"""Wave 1 browser-level red-team checks (Agent 9). xfail(strict=True) = real defect, see private/qa_wave1.md."""
from __future__ import annotations

import json
import re

import pytest

PAYLOADS = ['<img src=x onerror="window.__xss=1">', '"><svg onload="window.__xss=1">', "javascript:window.__xss=1", "</script><script>window.__xss=1</script>"]
DISPLAY_KEYS = {"name", "label", "unit", "dataset", "project_name", "smiles", "method_note", "limitations", "attribution_check", "note", "source", "license", "n", "model", "title", "site", "fixture_label", "method", "pos"}


def defect(fid, sev, msg):
    return pytest.mark.xfail(strict=True, reason="%s %s: %s" % (fid, sev, msg))


def _poison(obj, payload):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in DISPLAY_KEYS and isinstance(v, str):
                out[k] = payload + v
            elif k in ("changelog", "change_log") and isinstance(v, list):
                out[k] = [payload + x for x in v]
            else:
                out[k] = _poison(v, payload)
        return out
    if isinstance(obj, list):
        return [_poison(x, payload) for x in obj]
    return obj


def _install_poison(page, payload):
    def handler(route):
        resp = route.fetch()
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_poison(resp.json(), payload)))

    page.route("**/fixtures/*.json", handler)


@pytest.mark.parametrize("payload", PAYLOADS)
@pytest.mark.parametrize("url", ["app.html", "compound.html?c=3&e=herg", "model.html?e=herg", "docking.html"])
def test_hostile_fixture_strings_never_become_markup(make_page, url, payload):
    pg = make_page()
    _install_poison(pg, payload)
    pg.goto(pg.base + "/" + url)
    pg.wait_for_timeout(700)
    assert pg.evaluate("window.__xss") is None
    assert pg.evaluate("document.querySelectorAll('img[src=\"x\"], [onerror], [onload], body script:not([src])').length") == 0


def test_add_page_template_names_and_hostile_url_params_are_inert(make_page):
    pg = make_page()
    _install_poison(pg, PAYLOADS[0])
    pg.goto(pg.base + "/add.html")
    pg.click("#t-draw")
    pg.wait_for_timeout(500)
    pg.evaluate("document.getElementById('smi').value = '%s'" % PAYLOADS[1].replace("'", ""))
    pg.dispatch_event("#smi", "input")
    assert pg.evaluate("window.__xss") is None
    for q in ("compound.html?c=%3Cimg%20src%3Dx%20onerror%3Dwindow.__xss%3D1%3E&e=herg", "model.html?e=%3Cimg%20src%3Dx%20onerror%3Dwindow.__xss%3D1%3E"):
        pg.goto(pg.base + "/" + q)
        pg.wait_for_timeout(400)
        assert pg.evaluate("window.__xss") is None
        assert pg.evaluate("document.querySelectorAll('img[src=\"x\"]').length") == 0


def test_no_page_makes_a_third_party_request(make_page):
    for u in ["index.html", "auth.html", "app.html", "add.html", "compound.html?c=3&e=herg", "model.html", "docking.html", "account.html", "legal/terms.html"]:
        pg = make_page()
        pg.goto(pg.base + "/" + u)
        pg.wait_for_timeout(500)
        ext = [r for r in pg.requests if not r.startswith(pg.base) and not r.startswith(("data:", "blob:"))]
        assert not ext, (u, ext)


def test_consents_default_off_and_do_not_leak_to_other_state(make_page):
    pg = make_page()
    pg.goto(pg.base + "/account.html")
    pg.wait_for_selector("[data-consent]")
    assert pg.evaluate("[...document.querySelectorAll('[data-consent]')].every(c => !c.checked)")
    assert pg.evaluate("Object.keys(localStorage).every(k => k.startsWith('metis.fixture.consent.'))")


def test_signup_requires_terms_checkbox(make_page):
    pg = make_page()
    pg.goto(pg.base + "/auth.html")
    assert pg.is_disabled("#signup-submit")
    pg.check("#accept")
    assert pg.is_enabled("#signup-submit")


def test_login_form_never_puts_password_in_url_when_js_is_down(make_page):
    pg = make_page()
    pg.route("**/js/*.js", lambda r: r.abort())
    pg.goto(pg.base + "/auth.html")
    pg.evaluate("document.getElementById('p-login').hidden = false")
    pg.fill("#li-email", "a@b.co")
    pg.fill("#li-pass", "hunter2hunter2")
    pg.press("#li-pass", "Enter")
    pg.wait_for_timeout(400)
    assert "hunter2" not in pg.url and "password" not in pg.url


@pytest.mark.parametrize("url", ["compound.html?e=__proto__", "compound.html?e=constructor", "model.html?e=__proto__", "model.html?e=toString"])
def test_prototype_keys_in_query_do_not_break_pages(make_page, url):
    pg = make_page()
    pg.goto(pg.base + "/" + url)
    pg.wait_for_timeout(500)
    assert not pg.errors and "undefined" not in pg.inner_text("h1")


def test_docking_page_reflows_at_375px(make_page):
    pg = make_page(375, 700)
    pg.goto(pg.base + "/docking.html")
    pg.wait_for_timeout(600)
    assert not pg.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")


@pytest.mark.parametrize("url", ["index.html", "auth.html", "app.html", "add.html", "compound.html?c=3&e=herg", "model.html", "account.html"])
def test_other_pages_reflow_at_375px(make_page, url):
    pg = make_page(375, 700)
    pg.goto(pg.base + "/" + url)
    pg.wait_for_timeout(500)
    assert not pg.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")


def test_docking_inputs_have_accessible_names(make_page):
    pg = make_page()
    pg.goto(pg.base + "/docking.html")
    pg.wait_for_timeout(500)
    bad = pg.evaluate("""() => [...document.querySelectorAll('input,select,textarea')].filter(e => !(e.labels && e.labels.length && e.labels[0].control === e) && !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby')).map(e => e.id || e.name)""")
    assert not bad, bad


@pytest.mark.parametrize("url", ["app.html", "add.html", "compound.html?c=3&e=herg", "model.html", "docking.html", "account.html", "auth.html"])
def test_basic_a11y_one_h1_lang_alt_and_names(make_page, url):
    pg = make_page()
    pg.goto(pg.base + "/" + url)
    pg.wait_for_timeout(600)
    r = pg.evaluate("""() => ({h1: document.querySelectorAll('h1').length, lang: document.documentElement.lang,
        noalt: [...document.querySelectorAll('img')].filter(e => !e.hasAttribute('alt')).length,
        noname: [...document.querySelectorAll('button,a')].filter(e => !((e.innerText || e.getAttribute('aria-label') || '').trim())).length,
        dup: [...document.querySelectorAll('[id]')].map(e => e.id).filter((x, i, a) => a.indexOf(x) !== i).length})""")
    assert r == {"h1": 1, "lang": "en", "noalt": 0, "noname": 0, "dup": 0}


def test_uncertainty_panel_heading_order(make_page):
    pg = make_page()
    pg.goto(pg.base + "/compound.html?c=3&e=herg")
    pg.wait_for_timeout(600)
    hs = pg.evaluate("[...document.querySelectorAll('h1,h2,h3,h4')].map(h => +h.tagName[1])")
    assert all(b - a <= 1 for a, b in zip(hs, hs[1:])), hs


# ---------------- spec / wireframe conformance (passing) ----------------
def test_every_project_table_cell_shows_value_interval_n_coverage_and_sample_size(make_page):
    pg = make_page()
    pg.goto(pg.base + "/app.html")
    pg.wait_for_selector("td[data-pred]")
    cells = pg.evaluate("[...document.querySelectorAll('td[data-pred]')].map(td => ({t: td.innerText, n: td.dataset.n, c: td.dataset.coverage, s: td.dataset.sampleSize}))")
    assert len(cells) == 8 * 5
    for c in cells:
        assert re.search(r"N=\d+", c["t"]) and c["n"] and c["c"] and c["s"]
        assert re.search(r"\[[-\d.]+, [-\d.]+\]|\(cal\.\)", c["t"]) and re.search(r"% interval|calibrated probability", c["t"])
    heads = pg.inner_text("thead")
    assert "verdict" not in heads.lower() and "recommend" not in heads.lower()


def test_project_table_shows_2d_thumbs_3d_button_both_ranks_and_conformer_label(make_page):
    pg = make_page()
    pg.goto(pg.base + "/app.html")
    pg.wait_for_selector("td[data-pred]")
    assert pg.locator("tbody tr img.mol").count() == 8 and pg.locator("tbody tr button:has-text('3D')").count() == 8
    assert re.search(r"\d+ \(\d+\)", pg.inner_text("[data-testid=rank]"))
    pg.click("tbody tr:first-child button:has-text('3D')")
    assert "computed conformer, not an experimental structure" in pg.inner_text("#viewer-cap")


def test_uncertainty_panel_has_2d_and_3d_decomposition_ranges_and_method_note(make_page):
    pg = make_page()
    pg.goto(pg.base + "/compound.html?c=3&e=herg")
    pg.wait_for_selector("#shares [data-share]")
    t = pg.inner_text("main").lower()
    assert pg.locator("#d2 img").count() == 1 and pg.locator("#viewer").count() == 1
    assert pg.locator("#shares [data-share]").count() == 3 and "over resamples" in t and "method:" in t
    assert "not a measured quantity" in t and "computed conformer, not an experimental structure" in t
    assert pg.locator("#nearest .molcell").count() >= 3


def test_home_page_is_dark_even_when_visitor_prefers_light(make_page):
    pg = make_page(color_scheme="light")
    pg.goto(pg.base + "/index.html")
    assert pg.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(20, 24, 28)"
    assert pg.evaluate("[...document.querySelectorAll('*')].every(e => !/gradient|shadow/.test(getComputedStyle(e).backgroundImage + getComputedStyle(e).boxShadow.replace('none','')))")


def test_no_gradients_or_shadows_anywhere(make_page):
    for u in ["app.html", "compound.html?c=3&e=herg", "model.html", "account.html"]:
        pg = make_page()
        pg.goto(pg.base + "/" + u)
        pg.wait_for_timeout(500)
        assert pg.evaluate("[...document.querySelectorAll('body *')].filter(e => { const s = getComputedStyle(e); return /gradient/.test(s.backgroundImage) || (s.boxShadow !== 'none') || (s.textShadow !== 'none'); }).length") == 0, u


# ---------------- spec / wireframe conformance gaps ----------------
def test_model_page_reports_calibration_and_noise_ceiling(make_page):
    pg = make_page()
    pg.goto(pg.base + "/model.html?e=solubility")
    pg.wait_for_selector("#report table")
    t = pg.inner_text("#report").lower()
    for needle in ("reliability", "ece", "similarity bin", "noise ceiling"):
        assert needle in t, needle


@defect("G-02", "LOW", "uncertainty panel: for regression endpoints the nearest-training-compound captions say 'measured (fixture label): see model page' instead of the measured value that the wireframe/spec shows")
def test_nearest_compounds_show_measured_value_for_regression(make_page):
    pg = make_page()
    pg.goto(pg.base + "/compound.html?c=1&e=solubility")
    pg.wait_for_selector("#nearest .molcell")
    assert "see model page" not in pg.inner_text("#nearest")


@defect("G-04", "LOW", "project table shows only 5 of the v1 endpoints (no DILI, half-life, clearance, logP/logD columns) although the add page offers them and model pages exist")
def test_project_table_covers_every_selectable_endpoint(make_page):
    pg = make_page()
    pg.goto(pg.base + "/app.html")
    pg.wait_for_selector("td[data-pred]")
    heads = pg.inner_text("thead").lower()
    for h in ("dili", "half-life", "clearance", "logd"):
        assert h in heads, h


@defect("G-07", "LOW", "home page: the 3D complex is hard-cropped by its container (cartoon cut off at the top and right edge in screenshots/00_home.png) instead of fitting the view")
def test_home_3d_scene_is_not_clipped_by_container(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/index.html")
    pg.wait_for_selector("#mol3d canvas")
    pg.wait_for_timeout(1200)
    # screenshot the box, then inspect its outermost pixel ring: a fitted scene leaves it at the background colour
    import base64

    b64 = base64.b64encode(pg.locator("#mol3d").screenshot()).decode()
    ring = pg.evaluate("""async (b64) => { const img = new Image(); img.src = 'data:image/png;base64,' + b64; await img.decode();
        const c = document.createElement('canvas'); c.width = img.width; c.height = img.height; const g = c.getContext('2d'); g.drawImage(img, 0, 0);
        const w = c.width, h = c.height; let lit = 0; const bg = g.getImageData(2, 2, 1, 1).data;
        const far = (x, y) => { const p = g.getImageData(x, y, 1, 1).data; return Math.abs(p[0] - bg[0]) + Math.abs(p[1] - bg[1]) + Math.abs(p[2] - bg[2]) > 40; };
        for (let x = 0; x < w; x += 4) { if (far(x, 1)) lit++; if (far(x, h - 2)) lit++; }
        for (let y = 0; y < h; y += 4) { if (far(1, y)) lit++; if (far(w - 2, y)) lit++; }
        return lit; }""", b64)
    assert ring == 0
