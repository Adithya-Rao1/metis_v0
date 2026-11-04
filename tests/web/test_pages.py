"""Behaviour of the static screens in headless Chromium (Playwright)."""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
from mockapi import PID  # noqa: E402

PROJECT = f"app.html?p={PID}"
COMPOUND = f"compound.html?p={PID}&c=cccccccc-0000-4000-8000-000000000003&e=logd"
PAGES = ["index.html", "auth.html", "app.html", PROJECT, f"add.html?p={PID}", COMPOUND, "model.html?e=logd", "docking.html", "account.html", f"report.html?p={PID}"]
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
    pg.wait_for_timeout(600)
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390
    assert pg.evaluate("document.body.scrollWidth") <= 390


@pytest.mark.parametrize("page_url", PAGES[1:])
def test_basic_accessibility(make_page, page_url):
    pg = make_page(1280, 800)
    pg.goto(pg.base + "/" + page_url)
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(300)
    assert pg.evaluate("document.documentElement.lang") == "en"
    assert pg.title().strip() != ""
    assert pg.locator("h1:visible").count() == 1
    assert pg.evaluate("[...document.images].filter(i=>!i.hasAttribute('alt')).length") == 0
    unlabeled = pg.evaluate("""[...document.querySelectorAll('input:not([type=hidden]),select,textarea')].filter(e=>{
        if(e.getAttribute('aria-label')||e.getAttribute('aria-labelledby')) return false;
        if(e.closest('label')) return false; return !(e.id&&document.querySelector('label[for="'+e.id+'"]'));}).map(e=>e.id||e.name)""")
    assert unlabeled == []
    assert pg.evaluate("[...document.querySelectorAll('button')].filter(b=>!b.textContent.trim()&&!b.getAttribute('aria-label')).length") == 0
    assert pg.locator(".skip").count() == 1 or page_url.startswith("auth")
    assert pg.evaluate("document.querySelector('main')!==null")


@pytest.mark.parametrize("page_url", ["app.html", PROJECT, f"add.html?p={PID}", COMPOUND, "model.html?e=logd", "docking.html", "account.html", f"report.html?p={PID}"])
def test_screens_show_disclaimer_and_no_fixture_banner(make_page, page_url):
    pg = make_page(1280, 800)
    pg.goto(pg.base + "/" + page_url)
    pg.wait_for_load_state("networkidle")
    assert pg.inner_text("[data-testid=disclaimer]").strip() == DISCLAIMER
    assert pg.locator("[data-testid=fixture-banner]").count() == 0 and "FIXTURE" not in pg.inner_text("body")


def test_signed_out_visitor_is_sent_to_sign_in_and_back(make_page):
    from mockapi import MockApi
    pg = make_page(api=MockApi(signed_in=False))
    pg.goto(pg.base + "/" + PROJECT)
    pg.wait_for_url("**/auth.html?*")
    assert "next=app.html%3Fp%3D" in pg.url
    assert "Sign in to continue" in pg.inner_text("#auth-banner")
    assert pg.get_attribute("#tab-login", "aria-selected") == "true"


# ---------------- sign up / log in ----------------
def test_signup_requires_unchecked_by_default_acceptance(make_page):
    pg = make_page(1280, 800)
    pg.goto(pg.base + "/auth.html")
    pg.wait_for_function("() => document.getElementById('terms-version').textContent.includes('2026-01')")
    cb, btn = pg.locator("#accept"), pg.locator("#signup-submit")
    assert not cb.is_checked() and btn.is_disabled()
    assert pg.locator("[data-testid=draft-note]").count() == 0  # draft warning lives in BACKLOG, not on the page
    cb.check()
    assert btn.is_enabled()
    cb.uncheck()
    assert btn.is_disabled()
    label = pg.inner_text("#accept-label")
    for w in ("Terms of Service", "Privacy Policy", "Disclaimer", "version 2026-01"):
        assert w in label
    hrefs = pg.eval_on_selector_all("#accept-links a", "els=>els.map(e=>e.getAttribute('href'))")
    assert hrefs == ["legal/terms.html", "legal/privacy.html", "legal/disclaimer.html"]
    for h in hrefs:
        pg2 = pg.context.new_page()
        resp = pg2.goto(pg.base + "/" + h)
        assert resp.ok and "DRAFT" in pg2.inner_text("body")
        pg2.close()
    cb.focus(); pg.keyboard.press("Space")
    assert btn.is_enabled()
    pg.click("#tab-login")
    assert pg.locator("#login-submit").is_enabled()


def test_signup_sends_terms_version_and_csrf_header(make_page):
    pg = make_page(1280, 800)
    pg.goto(pg.base + "/auth.html")
    pg.wait_for_function("() => document.getElementById('terms-version').textContent.includes('2026-01')")
    pg.fill("#su-email", "new@example.org")
    pg.fill("#su-pass", "a long enough passphrase")
    pg.check("#accept")
    with pg.expect_request(lambda r: r.url.endswith("/auth/register")) as req:
        pg.click("#signup-submit")
    assert req.value.post_data_json == {"email": "new@example.org", "password": "a long enough passphrase", "accepted_terms_version": "2026-01"}
    assert req.value.headers["x-csrf-token"] == "testtoken"
    pg.wait_for_function("() => document.getElementById('signup-msg').textContent.includes('Check your email')")


def test_auth_error_messages_are_plain_language(make_page):
    from mockapi import MockApi
    api = MockApi(signed_in=False)
    pg = make_page(1280, 800, api=api)
    api.overrides[("POST", "/auth/login")] = (401, {"detail": "invalid_credentials"}, {})
    pg.goto(pg.base + "/auth.html")
    pg.click("#tab-login")
    pg.fill("#li-email", "a@b.co"); pg.fill("#li-pass", "x")
    pg.click("#login-submit")
    pg.wait_for_function("() => document.getElementById('login-msg').dataset.kind === 'error'")
    assert pg.inner_text("#login-msg") == "Email or password is not correct."
    api.overrides[("POST", "/auth/login")] = (403, {"detail": "email_not_verified"}, {})
    pg.click("#login-submit")
    pg.wait_for_function("() => document.getElementById('login-msg').textContent.includes('not verified')")
    api.overrides[("POST", "/auth/login")] = (429, {"detail": "rate_limited"}, {"retry-after": "120"})
    pg.click("#login-submit")
    pg.wait_for_function("() => document.getElementById('login-msg').textContent.includes('Too many')")
    assert "2 minutes" in pg.inner_text("#login-msg")
    api.overrides[("POST", "/auth/login")] = (503, {"detail": "busy"}, {"retry-after": "7"})
    pg.click("#login-submit")
    pg.wait_for_function("() => document.getElementById('login-msg').textContent.includes('busy')")
    assert "7 seconds" in pg.inner_text("#login-msg")
    api.overrides[("POST", "/auth/register")] = (422, {"detail": [{"loc": ["body", "password"], "msg": "Password is too easy to guess", "type": "x"}]}, {})
    pg.click("#tab-signup")
    pg.fill("#su-email", "a@b.co"); pg.fill("#su-pass", "x"); pg.check("#accept")
    pg.click("#signup-submit")
    pg.wait_for_selector("#signup .field-error")
    assert "too easy to guess" in pg.inner_text("#signup .field-error")


def test_verify_and_reset_links_consume_tokens_and_leave_the_url(make_page):
    from mockapi import MockApi
    api = MockApi(signed_in=False)
    api.overrides[("POST", "/auth/verify")] = (200, {"status": "verified"}, {})
    api.overrides[("POST", "/auth/reset")] = (200, {"status": "password_reset"}, {})
    pg = make_page(1280, 800, api=api)
    pg.goto(pg.base + "/auth.html?verify=" + "t" * 40)
    pg.wait_for_function("() => document.getElementById('verify-msg').textContent.includes('is verified')")
    assert "verify=" not in pg.url
    assert [c for c in api.calls if c[1] == "/auth/verify"][0][2] == {"token": "t" * 40}
    pg.goto(pg.base + "/auth.html?reset=" + "r" * 40)
    pg.fill("#rp-pass", "a brand new passphrase")
    pg.click("#reset button[type=submit]")
    pg.wait_for_function("() => document.getElementById('reset-msg').textContent.includes('Password changed')")
    assert [c for c in api.calls if c[1] == "/auth/reset"][0][2] == {"token": "r" * 40, "password": "a brand new passphrase"}
    api.overrides[("POST", "/auth/verify")] = (400, {"detail": "invalid_or_expired_token"}, {})
    pg.goto(pg.base + "/auth.html?verify=" + "t" * 40)
    pg.wait_for_function("() => document.getElementById('verify-msg').textContent.includes('expired')")
    assert pg.locator("#verify-resend").count() == 1


def test_changed_terms_must_be_reaccepted(make_page):
    from mockapi import MockApi
    api = MockApi()
    api.user = {**api.user, "terms_current": False}
    pg = make_page(1280, 800, api=api)
    pg.goto(pg.base + "/" + PROJECT)
    pg.wait_for_url("**/auth.html?*why=terms*")
    pg.wait_for_selector("#p-terms:not([hidden])")
    assert pg.is_disabled("#reaccept-submit") and not pg.is_checked("#reaccept-box")
    api.overrides[("POST", "/terms/accept")] = (200, {"status": "accepted", "version": "2026-01"}, {})
    pg.check("#reaccept-box")
    with pg.expect_request(lambda r: r.url.endswith("/terms/accept")) as req:
        pg.click("#reaccept-submit")
    assert req.value.post_data_json == {"version": "2026-01"}


# ---------------- project table ----------------
def test_projects_list_create_and_empty_state(make_page):
    from mockapi import MockApi
    pg = make_page(1280, 800, api=MockApi(projects=False))
    pg.goto(pg.base + "/app.html")
    pg.wait_for_selector("[data-state=empty]")
    assert "No projects yet" in pg.inner_text("#plist")
    pg2 = make_page(1280, 800)
    pg2.goto(pg2.base + "/app.html")
    pg2.wait_for_selector("[data-project]")
    assert "4 of 10 compounds" in pg2.inner_text("[data-testid=project-list]")


def test_error_state_has_retry(make_page):
    from mockapi import MockApi
    api = MockApi()
    api.overrides[("GET", "^/projects$")] = (503, {"detail": "down"}, {"retry-after": "5"})
    pg = make_page(1280, 800, api=api)
    pg.goto(pg.base + "/app.html")
    pg.wait_for_selector("[data-state=error]")
    assert "busy" in pg.inner_text("[data-state=error]") and "5 seconds" in pg.inner_text("[data-state=error]")
    del api.overrides[("GET", "^/projects$")]
    pg.click("[data-state=error] button")
    pg.wait_for_selector("[data-project]")


def test_prediction_cells_complete_and_neutral(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/" + PROJECT)
    pg.wait_for_selector("tr.sel-row")
    cells = pg.locator("[data-pred]")
    n = cells.count()
    assert n == 4 * 2
    for i in range(n):
        c = cells.nth(i)
        t = c.inner_text()
        kind = c.get_attribute("data-pred")
        assert re.search(r"N=\d+", t) and re.search(r"n=[\d,]+", t), t
        if kind == "ames":
            assert re.search(r"\d\.\d\d \(cal\.\)", t) and "calibrated probability" in t
        else:
            assert re.search(r"-?\d+\.\d \[-?\d+\.\d, -?\d+\.\d\]", t) and re.search(r"\d+% interval", t), t
        assert c.get_attribute("data-coverage") and c.get_attribute("data-n") and c.get_attribute("data-sample-size")
        html = c.inner_html()
        assert not re.search(r'class="[^"]*\b(ok|warn|bad|good|fail|pass|success|danger)\b', html)
        assert not re.search(r"\b(safe|unsafe|toxic|good|bad|promising|recommended)\b", t, re.I)
    heads = pg.eval_on_selector_all("#thead th", "els=>els.map(e=>e.textContent)")
    assert not any(re.search("verdict|status|flag", h, re.I) for h in heads)
    assert pg.locator("#thead a[href='model.html?e=logd']").count() == 1
    assert pg.locator("[data-pred=logd] a").first.get_attribute("href").startswith(f"compound.html?p={PID}&c=")
    assert pg.evaluate("[...document.querySelectorAll('[data-pred], [data-pred] *')].some(e=>{const s=getComputedStyle(e);return s.boxShadow!=='none'||s.textShadow!=='none'||s.backgroundImage!=='none'})") is False


def test_table_without_predictions_or_compounds_says_so(make_page):
    from mockapi import MockApi
    pg = make_page(1280, 800, api=MockApi(predicted=False))
    pg.goto(pg.base + "/" + PROJECT)
    pg.wait_for_selector("tr.sel-row")
    assert "No predictions yet" in pg.inner_text("#ptable-state")
    assert pg.locator("[data-pred]").count() == 0 and pg.is_disabled("#rank-save")
    pg2 = make_page(1280, 800, api=MockApi(projects=False))
    pg2.goto(pg2.base + "/app.html?p=aaaaaaaa-0000-4000-8000-000000000001")
    pg2.wait_for_selector("[data-state=empty]")
    assert "no compounds yet" in pg2.inner_text("#ptable-state")


def test_2d_everywhere_and_one_shared_3d_viewer(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/" + PROJECT)
    pg.wait_for_selector("tr.sel-row")
    assert pg.locator("tr.sel-row img.mol").count() == 4 and pg.locator("tr.sel-row button:has-text('3D')").count() == 4
    assert pg.evaluate("[...document.querySelectorAll('tr.sel-row img.mol')].every(i=>i.complete && i.naturalWidth>0)")
    for i in (0, 2, 3):
        pg.locator("tr.sel-row button:has-text('3D')").nth(i).click()
        pg.wait_for_timeout(300)
    pg.wait_for_function("() => document.getElementById('viewer').dataset.shown === '1'")
    assert pg.locator("canvas").count() == 1
    assert "computed conformer, not an experimental structure" in pg.inner_text("#viewer-cap")
    assert pg.get_attribute("#viewer", "data-webgl") == "on"


def test_webgl_missing_keeps_2d_on_every_3d_screen(make_page):
    for url in [PROJECT, COMPOUND, "docking.html"]:
        pg = make_page(1280, 800)
        pg.add_init_script(NO_WEBGL)
        pg.goto(pg.base + "/" + url)
        pg.wait_for_selector("[data-testid=webgl-notice]")
        assert pg.locator("canvas").count() == 0
        assert "2D" in pg.inner_text("[data-testid=webgl-notice]")
        if url != "docking.html":
            pg.wait_for_selector("img.mol")
            assert pg.locator("img.mol").count() >= 1, url
        assert not [e for e in pg.errors if "Cannot" in e or "undefined" in e], pg.errors


def test_keyboard_only_through_the_project_table(make_page):
    pg = make_page(1280, 800)
    pg.goto(pg.base + "/" + PROJECT)
    pg.wait_for_selector("tr.sel-row")
    for _ in range(40):
        pg.keyboard.press("Tab")
        if pg.evaluate("document.activeElement && document.activeElement.matches('tr.sel-row')"):
            break
    assert pg.evaluate("document.activeElement.matches('tr.sel-row')")
    pg.keyboard.press("Enter")
    assert pg.evaluate("document.activeElement.getAttribute('aria-selected')") == "true"
    pg.keyboard.press("Tab")                      # the row's 3D button
    assert pg.evaluate("document.activeElement.textContent") == "3D"
    pg.keyboard.press("Enter")
    pg.wait_for_function("() => document.getElementById('viewer').dataset.shown === '1'")
    assert "computed conformer" in pg.inner_text("#viewer-cap")


def test_ranking_panel_defaults_and_save(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/" + PROJECT)
    pg.wait_for_selector("tr.sel-row")
    assert pg.get_attribute("#lam", "value") == "25" and pg.inner_text("#lamv") == "0.25"
    assert pg.get_attribute("#lam", "min") == "0" and pg.get_attribute("#lam", "max") == "100"
    assert "Best properties first" in pg.inner_text("#rankpanel") and "Least uncertain first" in pg.inner_text("#rankpanel")
    assert pg.input_value("[data-tmin=logd]") == "-2" and pg.input_value("[data-tmax=logd]") == "6"
    assert "model default: -2 to 6" in pg.inner_text("[data-endpoint=logd] .rank-default")
    pg.fill("[data-limit-max=ames]", "0.5")
    pg.fill("[data-tmin=logd]", "-1")
    pg.fill("[data-tmax=logd]", "5")
    pg.locator("#lam").fill("80")
    pg.click("#rank-save")
    pg.wait_for_function("() => document.getElementById('rank-msg').textContent.startsWith('Saved')")
    sent = [c for c in pg.api.calls if c[0] == "PUT"][-1][2]
    assert sent["lam"] == 0.8 and sent["limits"] == [{"endpoint": "ames", "min": None, "max": 0.5, "use_unfavorable_bound": False}]
    w = {x["endpoint"]: x for x in sent["weights"]}
    assert w["logd"]["target_min"] == -1 and w["logd"]["target_max"] == 5 and "target_min" not in w["ames"]
    ranks = pg.eval_on_selector_all("[data-testid=rank]", "els=>els.map(e=>e.firstChild.textContent)")
    assert all(re.fullmatch(r"\d \(\d\)", r) for r in ranks)
    nfail = int(pg.inner_text("[data-testid=nfail]").split()[0])
    assert nfail >= 1 and pg.locator("[data-testid=fail]").count() == nfail
    for i in range(nfail):
        row = pg.locator("tr.sel-row:has([data-testid=fail])").nth(i)
        assert re.match(r"\d \(\d\)", row.locator("[data-testid=rank]").inner_text()) and "limit: < 0.5" in row.inner_text()
    shares = [float(x) for x in pg.eval_on_selector_all("#bd-table tbody tr td:last-child", "els=>els.map(e=>e.textContent)")]
    big_r = float(re.search(r"R = ([\d.]+)", pg.inner_text("#bd-title")).group(1))
    assert len(shares) == 2 and abs(sum(shares) - big_r) < 0.0035
    pg.click("[data-endpoint=logd] button:has-text('Reset range')")
    assert pg.input_value("[data-tmin=logd]") == "-2"


def test_ranking_422_message_is_shown(make_page):
    from mockapi import MockApi
    api = MockApi()
    pg = make_page(1280, 800, api=api)
    pg.goto(pg.base + "/" + PROJECT)
    pg.wait_for_selector("tr.sel-row")
    api.overrides[("PUT", "/ranking")] = (422, {"detail": "at least one weight must be positive"}, {})
    pg.click("#rank-save")
    pg.wait_for_function("() => document.getElementById('rank-msg').dataset.kind === 'error'")
    assert "at least one weight must be positive" in pg.inner_text("#rank-msg")


# ---------------- add compounds ----------------
def test_add_compounds_cap_ensemble_time_and_consent(make_page):
    pg = make_page(1280, 800)
    pg.goto(pg.base + f"/add.html?p={PID}")
    pg.wait_for_selector("#smi")
    assert "Up to 10 compounds" in pg.inner_text("[data-testid=cap-note]")
    opts = lambda: pg.eval_on_selector_all("#ens option", "els=>els.map(e=>[e.value,e.disabled,e.selected])")
    assert opts() == [["3", False, False], ["5", False, True], ["10", False, False], ["20", True, False]]
    assert "free" in pg.inner_text("[data-testid=tier-note]") and "N = 10" in pg.inner_text("[data-testid=tier-note]")
    assert "Estimated time: about" in pg.inner_text("[data-testid=eta]")
    pg.fill("#smi", "\n".join(f"{'C' * (i + 1)} m{i}" for i in range(12)))
    assert "10 of 10 slots used (4 already" in pg.inner_text("[data-testid=slots]")
    assert pg.locator("#check .tag[data-kind=error]").count() == 6
    pg.fill("#smi", "CC(=O\nC1CC\nCC(=O)O")
    assert pg.locator("#check .tag[data-kind=error]").count() == 2
    assert "private by default" in pg.inner_text("[data-testid=consent-line]") and "opt in" in pg.inner_text("[data-testid=consent-line]")
    pg.focus("#t-paste"); pg.keyboard.press("ArrowRight"); pg.keyboard.press("ArrowRight"); pg.keyboard.press("ArrowRight")
    assert pg.get_attribute("#t-draw", "aria-selected") == "true"


def test_edu_tier_unlocks_n20_and_only_calibrated_sizes_are_offered(make_page):
    from mockapi import MockApi
    pg = make_page(1280, 800, api=MockApi(tier="edu"))
    pg.goto(pg.base + f"/add.html?p={PID}")
    pg.wait_for_selector("#ens option", state="attached")
    assert [o[1] for o in pg.eval_on_selector_all("#ens option", "els=>els.map(e=>[e.value,e.disabled])")] == [False] * 4


def test_add_sends_names_as_csv_and_shows_per_record_problems(make_page):
    from mockapi import MockApi
    api = MockApi()
    pg = make_page(1280, 800, api=api)
    pg.goto(pg.base + f"/add.html?p={PID}")
    pg.fill("#smi", "CCO ethanol\nCCN")
    api.overrides[("POST", "/compounds$")] = (422, {"detail": {"message": "some records were rejected; nothing was added", "problems": [{"record": 2, "error": "could not be parsed as a structure"}]}}, {})
    pg.click("#add-submit")
    pg.wait_for_selector("#add-problems .tag")
    assert "record 2: could not be parsed" in pg.inner_text("#add-problems")
    sent = [c for c in api.calls if c[1].endswith("/compounds")][-1][2]
    assert sent["format"] == "csv" and sent["content"].startswith('smiles,name\n"CCO","ethanol"')


def test_no_models_state_disables_prediction(make_page):
    from mockapi import MockApi
    pg = make_page(1280, 800, api=MockApi(models=False))
    pg.goto(pg.base + f"/add.html?p={PID}")
    pg.wait_for_selector("#no-models")
    assert "No models are published yet" in pg.inner_text("#no-models") and pg.is_disabled("#predict-submit")


def test_predict_progress_and_busy_messages(make_page):
    from mockapi import MockApi
    api = MockApi()
    pg = make_page(1280, 800, api=api)
    pg.goto(pg.base + f"/add.html?p={PID}")
    pg.wait_for_selector("#ens option", state="attached")
    api.overrides[("POST", "/predict$")] = (429, {"detail": "too many prediction jobs"}, {"retry-after": "10"})
    pg.click("#predict-submit")
    pg.wait_for_selector("#job .state-error")
    assert "Too many" in pg.inner_text("#job") and "10 seconds" in pg.inner_text("#job")
    del api.overrides[("POST", "/predict$")]
    pg.click("#predict-submit")
    pg.wait_for_selector("#to-table")
    assert "done: 8 of 8" in pg.inner_text("[data-testid=job-line]") and "Server estimate" in pg.inner_text("[data-testid=job-estimate]")


# ---------------- uncertainty panel ----------------
def test_uncertainty_panel_content(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/" + COMPOUND)
    pg.wait_for_selector("[data-testid=panel-value]")
    assert re.search(r"-?\d+\.\d \[-?\d+\.\d, -?\d+\.\d\]", pg.inner_text("[data-testid=panel-value]"))
    meta = pg.inner_text("[data-testid=panel-meta]")
    assert "90% interval" in meta and "N = 5" in meta and "1,200" in meta
    pcts = [int(x) for x in re.findall(r"(\d+)%", " ".join(pg.eval_on_selector_all("#shares [data-share] .mono", "e=>e.map(x=>x.textContent)")))]
    assert pcts == [50, 30, 20]
    assert pg.get_attribute("#viewer", "data-webgl") == "on"
    pg.locator("#fragments button").first.click()
    pg.wait_for_function("() => document.querySelector('#d2 img').src.includes('highlight=0&highlight=1')")
    assert "Tanimoto 0.50" in pg.inner_text("#nearest")
    assert "describes model behavior, not chemistry" in pg.inner_text("main")
    assert pg.get_attribute("#modellink", "href") == "model.html?e=logd"
    pg.goto(pg.base + "/" + COMPOUND.replace("e=logd", "e=ames").replace("000000000003", "000000000002"))
    pg.wait_for_selector("[data-testid=panel-value]")
    assert re.fullmatch(r"\d\.\d\d", pg.inner_text("[data-testid=panel-value]")) and "Calibrated probability" in pg.inner_text("#valcard")


def test_uncertainty_409_and_missing_prediction(make_page):
    from mockapi import MockApi
    api = MockApi()
    api.overrides[("GET", "/uncertainty$")] = (409, {"detail": "model version has changed since this prediction was made"}, {})
    pg = make_page(1280, 800, api=api)
    pg.goto(pg.base + "/" + COMPOUND)
    pg.wait_for_selector("[data-state=error]")
    assert "model version has changed" in pg.inner_text("[data-state=error]")
    pg2 = make_page(1280, 800, api=MockApi(predicted=False))
    pg2.goto(pg2.base + "/" + COMPOUND)
    pg2.wait_for_selector("[data-state=empty]")
    assert "No prediction exists" in pg2.inner_text("#page-state")


# ---------------- model page ----------------
def test_model_page_fields(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/model.html?e=logd")
    pg.wait_for_selector("#coverage-table")
    t = pg.inner_text("#report")
    assert "Mock dataset" in t and "CC0 (mock)" in t and "Known limitations" in t and "scaffold" in pg.inner_text("[data-testid=split]").lower()
    assert pg.locator("#coverage-table tbody tr").count() == 4
    assert pg.locator("#benchmark-table tbody tr").count() == 3          # rmse.model, rmse.baseline, n_test
    assert "-2 to 6" in pg.inner_text("[data-testid=default-range]")
    assert pg.locator("#picker a").count() == 2


def test_model_page_empty_state_is_honest(make_page):
    from mockapi import MockApi
    pg = make_page(1280, 800, api=MockApi(models=False))
    pg.goto(pg.base + "/model.html")
    pg.wait_for_selector("[data-state=empty]")
    t = pg.inner_text("#report")
    assert "No models have been published yet" in t and "placeholder" in t and not re.search(r"\d\.\d", t)


# ---------------- docking ----------------
def test_docking_prep_form_and_errors(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/docking.html")
    pg.wait_for_selector("#dock-form")
    assert "Does not run docking" in pg.inner_text("main") and "PDBQT" in pg.inner_text("#eng-note")
    pg.check("input[name=eng][value=gnina]")
    assert "GNINA" in pg.inner_text("#eng-note") and "SDF" in pg.inner_text("#eng-note")
    assert pg.get_attribute("#viewer", "data-webgl") == "on"
    pg.click("#prep")
    assert "PDB ID" in pg.inner_text("#prep-msg")
    pg.fill("#pdbid", "1M17")
    pg.click("#prep")
    pg.wait_for_selector("#result .state-error")
    assert "upload the file instead" in pg.inner_text("#prep-msg")
    pg.check("#src-up")
    assert pg.is_enabled("#up") and pg.is_disabled("#pdbid")


# ---------------- account ----------------
def test_account_consents_default_off_and_delete(make_page):
    pg = make_page(1280, 800)
    pg.goto(pg.base + "/account.html")
    pg.wait_for_selector("[data-consent]")
    boxes = pg.locator("[data-consent]")
    assert boxes.count() == 4
    assert all(not boxes.nth(i).is_checked() for i in range(4))
    assert "30 days" in pg.inner_text("main") and "Delete my account" in pg.inner_text("main")
    assert "free" in pg.inner_text("[data-testid=acct-tier]") and "N = 10" in pg.inner_text("[data-testid=acct-cap]") and ".edu" in pg.inner_text("#edu-note")
    pg.check("#consent-analytics")
    pg.wait_for_function("() => document.getElementById('cons-msg').textContent.includes('is now on')")
    assert [c for c in pg.api.calls if c[1] == "/consents" and c[0] == "POST"][-1][2] == {"scope": "analytics", "granted": True}
    pg.click("#del-all")
    assert pg.locator("#cd-ok").is_disabled()
    pg.fill("#cd-typed", "DELETE")
    assert pg.locator("#cd-ok").is_enabled()
    pg.click("#cd-cancel")
    assert pg.locator("dialog").count() == 0


# ---------------- export ----------------
def test_report_page_previews_csv(make_page):
    pg = make_page(1280, 800)
    pg.goto(pg.base + f"/report.html?p={PID}")
    pg.wait_for_selector("#csv-prev tbody tr")
    assert "disclaimer" in pg.inner_text("[data-testid=csv-preamble]")
    assert pg.eval_on_selector_all("#csv-prev thead th", "e=>e.map(x=>x.textContent)") == ["compound", "label"]
    assert pg.locator("[data-dl]").count() == 3
