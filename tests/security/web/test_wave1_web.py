"""Wave 1 browser-level red-team checks (Agent 9), ported in Wave 3 from fixtures to the real-API pages.

The pages now talk to `/api`; tests/web/mockapi.py (test-only data) answers it here (`api_page` fixture), and the server-side
invariants are checked against the real registry/model cards. xfail(strict=True) = real defect, see private/qa_wave3.md.
"""
from __future__ import annotations

import re

import pytest
from hostile_api import PAYLOADS, CardApi, PoisonApi, RichApi, card_json  # noqa: F401
from mockapi import PID

PROJECT = f"app.html?p={PID}"
COMPOUND = f"compound.html?p={PID}&c=cccccccc-0000-4000-8000-000000000003&e=logd"


def defect(fid, sev, msg):
    return pytest.mark.xfail(strict=True, reason="%s %s: %s" % (fid, sev, msg))


@pytest.mark.parametrize("payload", PAYLOADS)
@pytest.mark.parametrize("url", ["app.html", PROJECT, f"add.html?p={PID}", COMPOUND, "model.html?e=logd", "docking.html", "account.html", f"report.html?p={PID}"])
def test_hostile_api_strings_never_become_markup(api_page, url, payload):
    """Every display string the API returns carries the payload; none may become markup, script, a javascript: link or a handler."""
    pg = api_page(api=PoisonApi(payload))
    pg.goto(pg.base + "/" + url)
    pg.wait_for_timeout(900)
    assert url == "account.html" or payload in pg.inner_text("body"), "the hostile text must reach the page (as text), otherwise this test proves nothing"
    assert pg.evaluate("window.__xss") is None
    assert pg.evaluate("document.querySelectorAll('img[src=\"x\"], [onerror], [onload], body script:not([src])').length") == 0
    assert pg.evaluate("[...document.querySelectorAll('a[href],iframe[src],form[action]')].filter(e => /^\\s*javascript:/i.test(e.getAttribute('href') || e.getAttribute('src') || e.getAttribute('action'))).length") == 0


def test_add_page_template_names_and_hostile_url_params_are_inert(api_page):
    """Same invariant as Wave 1 (typed text and URL parameters are inert) on the wired pages: hostile project/compound names,
    a hostile SMILES typed into the paste box, and hostile ids/endpoints in the query string."""
    pg = api_page(api=PoisonApi(PAYLOADS[0]))
    pg.goto(f"{pg.base}/add.html?p={PID}")
    pg.wait_for_selector("#add-frame:not([hidden])")
    pg.click("#t-draw")
    pg.wait_for_timeout(500)
    pg.click("#t-paste")
    pg.evaluate("document.getElementById('smi').value = '%s'" % PAYLOADS[1].replace("'", ""))
    pg.dispatch_event("#smi", "input")
    pg.wait_for_timeout(200)
    assert pg.evaluate("window.__xss") is None
    assert pg.evaluate("document.querySelectorAll('#check [onload], #check svg, #check img').length") == 0
    hostile = "%3Cimg%20src%3Dx%20onerror%3Dwindow.__xss%3D1%3E"
    for q in (f"compound.html?p={hostile}&c={hostile}&e={hostile}", f"compound.html?p={PID}&c=cccccccc-0000-4000-8000-000000000003&e={hostile}",
              f"model.html?e={hostile}", f"app.html?p={hostile}", f"add.html?p={hostile}", f"report.html?p={hostile}", f"auth.html?next={hostile}&why={hostile}&verify={hostile}"):
        pg.goto(pg.base + "/" + q)
        pg.wait_for_timeout(400)
        assert pg.evaluate("window.__xss") is None, q
        assert pg.evaluate("document.querySelectorAll('img[src=\"x\"]').length") == 0, q


def test_no_page_makes_a_third_party_request(make_page):
    for u in ["index.html", "auth.html", "app.html", "add.html", "compound.html?c=3&e=herg", "model.html", "docking.html", "account.html", "legal/terms.html"]:
        pg = make_page()
        pg.goto(pg.base + "/" + u)
        pg.wait_for_timeout(500)
        ext = [r for r in pg.requests if not r.startswith(pg.base) and not r.startswith(("data:", "blob:"))]
        assert not ext, (u, ext)


def test_consents_default_off_and_do_not_leak_to_other_state(api_page):
    """All four consents start off, toggling one changes only that scope, and nothing is kept in browser storage."""
    pg = api_page()
    pg.goto(pg.base + "/account.html")
    pg.wait_for_selector("[data-consent]")
    assert pg.locator("[data-consent]").count() == 4
    assert pg.evaluate("[...document.querySelectorAll('[data-consent]')].every(c => !c.checked)")
    pg.check("#consent-analytics")
    pg.wait_for_function("() => document.getElementById('cons-msg').textContent.includes('is now on')")
    assert pg.evaluate("[...document.querySelectorAll('[data-consent]')].filter(c => c.checked).map(c => c.dataset.consent)") == ["analytics"]
    posts = [c for c in pg.api.calls if c[0] == "POST" and c[1] == "/consents"]
    assert [c[2] for c in posts] == [{"scope": "analytics", "granted": True}]
    assert pg.evaluate("Object.keys(localStorage).length + Object.keys(sessionStorage).length") == 0


def test_signup_requires_terms_checkbox(api_page):
    pg = api_page()
    pg.goto(pg.base + "/auth.html")
    pg.wait_for_function("() => document.getElementById('terms-version').textContent.includes('version')")
    assert pg.is_disabled("#signup-submit") and not pg.is_checked("#accept")
    pg.fill("#su-email", "a@example.org")
    pg.fill("#su-pass", "correct horse battery")
    pg.evaluate("document.getElementById('signup').requestSubmit()")   # a scripted submit must not get around the box
    pg.wait_for_timeout(300)
    assert not [c for c in pg.api.calls if c[1] == "/auth/register"]
    pg.check("#accept")
    assert pg.is_enabled("#signup-submit")
    pg.uncheck("#accept")
    assert pg.is_disabled("#signup-submit")


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


@pytest.mark.parametrize("key", ["__proto__", "constructor", "toString", "hasOwnProperty"])
@pytest.mark.parametrize("url", [f"compound.html?p={PID}&c=cccccccc-0000-4000-8000-000000000003&e=%s", "compound.html?e=%s", "model.html?e=%s"])
def test_prototype_keys_in_query_do_not_break_pages(api_page, url, key):
    pg = api_page()
    pg.goto(pg.base + "/" + url % key)
    pg.wait_for_timeout(600)
    assert not pg.errors, pg.errors
    assert "undefined" not in pg.inner_text("h1") and "[object" not in pg.inner_text("main")


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


@pytest.mark.parametrize("url", ["app.html", PROJECT, f"add.html?p={PID}", COMPOUND, "model.html?e=logd", "docking.html", "account.html", "auth.html", f"report.html?p={PID}"])
def test_basic_a11y_one_h1_lang_alt_and_names(api_page, url):
    # app.html holds two views, one `hidden`: only exposed headings count
    pg = api_page()
    pg.goto(pg.base + "/" + url)
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(600)
    r = pg.evaluate("""() => ({h1: [...document.querySelectorAll('h1')].filter(h => !h.closest('[hidden]')).length, lang: document.documentElement.lang,
        noalt: [...document.querySelectorAll('img')].filter(e => !e.hasAttribute('alt')).length,
        noname: [...document.querySelectorAll('button,a')].filter(e => !((e.innerText || e.getAttribute('aria-label') || '').trim())).length,
        dup: [...document.querySelectorAll('[id]')].map(e => e.id).filter((x, i, a) => a.indexOf(x) !== i).length})""")
    assert r == {"h1": 1, "lang": "en", "noalt": 0, "noname": 0, "dup": 0}


def test_uncertainty_panel_heading_order(api_page):
    pg = api_page()
    pg.goto(pg.base + "/" + COMPOUND)
    pg.wait_for_selector("#panel-frame:not([hidden])")
    hs = pg.evaluate("[...document.querySelectorAll('h1,h2,h3,h4')].map(h => +h.tagName[1])")
    assert all(b - a <= 1 for a, b in zip(hs, hs[1:])), hs


# ---------------- spec / wireframe conformance (passing) ----------------
def test_every_project_table_cell_shows_value_interval_n_coverage_and_sample_size(api_page):
    pg = api_page()
    pg.goto(pg.base + "/" + PROJECT)
    pg.wait_for_selector("td[data-pred]")
    cells = pg.evaluate("[...document.querySelectorAll('td[data-pred]')].map(td => ({t: td.innerText, n: td.dataset.n, c: td.dataset.coverage, s: td.dataset.sampleSize}))")
    assert len(cells) == 4 * 2        # 4 compounds x 2 endpoints, no cell missing
    for c in cells:
        assert re.search(r"N=\d+", c["t"]) and c["n"] and c["c"] and c["s"]
        assert re.search(r"\[[-\d.]+, [-\d.]+\]|\(cal\.\)", c["t"]) and re.search(r"% interval|calibrated probability", c["t"])
    heads = pg.inner_text("thead")
    assert "verdict" not in heads.lower() and "recommend" not in heads.lower()


def test_project_table_shows_2d_thumbs_3d_button_both_ranks_and_conformer_label(api_page):
    pg = api_page()
    pg.goto(pg.base + "/" + PROJECT)
    pg.wait_for_selector("td[data-pred]")
    assert pg.locator("tbody tr img.mol").count() == 4 and pg.locator("tbody tr button:has-text('3D')").count() == 4
    pg.wait_for_function("() => [...document.querySelectorAll('tbody tr img.mol')].every(i => i.complete && i.naturalWidth > 0)")
    pg.click("#rank-save")                                    # ranks exist only after the server has ranked
    pg.wait_for_function("() => /\\d+ \\(\\d+\\)/.test(document.querySelector('[data-testid=rank]').innerText)")
    ranks = pg.eval_on_selector_all("[data-testid=rank]", "els => els.map(e => e.innerText)")
    assert len(ranks) == 4 and all(re.match(r"\d+ \(\d+\)", r) for r in ranks)          # blended (property-only), both ranks
    assert "blended" in pg.inner_text("thead") and "property-only" in pg.inner_text("thead")
    pg.click("tbody tr:first-child button:has-text('3D')")
    pg.wait_for_function("() => document.getElementById('viewer-cap').textContent.includes('computed conformer')")
    assert "computed conformer, not an experimental structure" in pg.inner_text("#viewer-cap")


def test_uncertainty_panel_has_2d_and_3d_decomposition_ranges_and_method_note(api_page):
    pg = api_page(api=RichApi())
    pg.goto(pg.base + "/" + COMPOUND)
    pg.wait_for_selector("#shares [data-share]")
    pg.wait_for_function("() => document.getElementById('viewer-cap').textContent.includes('computed conformer')")
    t = pg.inner_text("main").lower()
    assert pg.locator("#d2 img").count() == 1 and pg.locator("#viewer").count() == 1
    assert pg.locator("#shares [data-share]").count() == 3 and "method:" in t and "not a measured quantity" in t
    assert "computed conformer, not an experimental structure" in t
    assert pg.locator("#nearest [data-nearest]").count() >= 3
    assert re.search(r"range \d+ to \d+%", pg.inner_text("#fragments"))          # fragment shares carry their range
    pg.locator("#fragments button").first.click()
    pg.wait_for_function("() => document.querySelector('#d2 img').src.includes('highlight=')")


@pytest.mark.parametrize("tier,enabled", [("free", {"3", "5", "10"}), ("edu", {"3", "5", "10", "20"})])
def test_uncertainty_rerun_selector_lists_every_size_and_disables_only_unavailable_ones(api_page, tier, enabled):
    from mockapi import MockApi

    pg = api_page(api=MockApi(tier=tier))
    pg.goto(pg.base + "/" + COMPOUND)
    pg.wait_for_selector("#ens2 option", state="attached")
    opts = pg.evaluate("[...document.querySelectorAll('#ens2 option')].map(o => [o.value, o.disabled])")
    assert [v for v, _ in opts] == ["3", "5", "10", "20"]
    assert {v for v, d in opts if not d} == enabled


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
def test_model_page_reports_calibration_and_noise_ceiling(api_page, synth_registry):
    """Rendered from the cards a real registry publishes (synthetic models): regression pages show coverage by similarity bin and
    the noise-ceiling status, classification pages the reliability/ECE figures."""
    reg, _ = synth_registry
    cards = [card_json(reg, e) for e in ("synreg", "syncls")]
    texts = {}
    for ep in ("synreg", "syncls"):
        pg = api_page(api=CardApi(cards))
        pg.goto(f"{pg.base}/model.html?e={ep}")
        pg.wait_for_selector("#benchmark-table")
        texts[ep] = pg.inner_text("#report").lower().replace("_", " ")
    for needle in ("noise ceiling", "similarity bin", "coverage by n"):
        assert needle in texts["synreg"], needle
    for needle in ("reliability", "ece"):
        assert needle in texts["syncls"], needle


@defect("F-120", "LOW", "model page prints the reliability bins and the similarity-bin coverage as '[object Object]' (flatten() joins arrays of objects with String()), so the numbers the spec asks for (reliability diagram data, coverage by similarity bin) are on the card but unreadable on the page")
def test_model_page_renders_reliability_and_similarity_bins_as_numbers(api_page, synth_registry):
    reg, _ = synth_registry
    pg = api_page(api=CardApi([card_json(reg, e) for e in ("synreg", "syncls")]))
    for ep in ("synreg", "syncls"):
        pg.goto(f"{pg.base}/model.html?e={ep}")
        pg.wait_for_selector("#benchmark-table")
        assert "[object" not in pg.inner_text("#benchmark-table"), ep


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
