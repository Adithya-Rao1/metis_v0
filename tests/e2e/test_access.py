"""Access control, error messages, keyboard, phone width, WebGL fallback, CSP, accounts flows: real stack, real browser."""

from __future__ import annotations

import re

import pytest
from helpers import PW, account, create_project, log_in, new_email, sign_up
from test_journey import add_and_predict, csp_errors

from backend.accounts.config import AccountsConfig

NO_WEBGL = "const g=HTMLCanvasElement.prototype.getContext;HTMLCanvasElement.prototype.getContext=function(t,...a){if(/webgl/.test(t))return null;return g.call(this,t,...a)};"


@pytest.fixture(scope="module")
def owner(browser, stack):
    """One signed-in user with a predicted project, shared by the read-only checks below."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    pg.errors, pg.base = [], stack.url
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    email = account(pg, stack, "owner")
    pid = create_project(pg, "Private project")
    add_and_predict(pg, pid)
    yield ctx, pid, email
    ctx.close()


def test_second_user_cannot_see_the_first_users_project(owner, make_page, stack):
    ctx, pid, _ = owner
    other = make_page()
    account(other, stack, "intruder")
    other.goto(f"{other.base}/app.html?p={pid}")
    other.wait_for_selector("[data-state=error]")
    assert "Not found" in other.inner_text("#page-state")
    assert "Private project" not in other.inner_text("body")
    other.goto(f"{other.base}/app.html")
    other.wait_for_selector("[data-state=empty]")  # their own list is empty
    assert "Private project" not in other.inner_text("body")
    for page_url in (f"add.html?p={pid}", f"report.html?p={pid}"):
        other.goto(f"{other.base}/{page_url}")
        other.wait_for_selector("[data-state=error]")
        assert "Not found" in other.inner_text("#page-state")
    # direct API calls: 404, never 403, and no body that confirms the project exists
    for path in (
        f"/api/projects/{pid}",
        f"/api/projects/{pid}/ranking",
        f"/api/projects/{pid}/export?format=csv",
    ):
        r = other.request.get(other.base + path)
        assert r.status == 404, (path, r.status)


def test_signed_out_visitor_is_sent_to_sign_in(make_page):
    pg = make_page()
    pg.goto(pg.base + "/app.html")
    pg.wait_for_url("**/auth.html?*")
    assert "Sign in to continue" in pg.inner_text("#auth-banner")


def test_logout_ends_the_session(make_page, stack):
    pg = make_page()
    account(pg, stack, "logout")
    pg.goto(pg.base + "/account.html")
    pg.click("#logout")
    pg.wait_for_url("**/auth.html")
    pg.goto(pg.base + "/app.html")
    pg.wait_for_url("**/auth.html?*")


def test_rate_limit_429_message_with_retry_after(make_page, stack):
    pg = make_page()
    pg.goto(pg.base + "/auth.html#forgot")
    email = new_email("rl")
    msgs = []
    for _ in range(4):  # the per-address limit is 3 per hour
        pg.fill("#fg-email", email)
        pg.click("#forgot button[type=submit]")
        pg.wait_for_function("() => !document.getElementById('forgot-msg').textContent.startsWith('Working')")
        msgs.append(pg.inner_text("#forgot-msg"))
    assert msgs[0].startswith("If that address belongs to a verified account")
    assert "Too many attempts. Try again in" in msgs[3], msgs
    assert re.search(r"Try again in \d+ minutes", msgs[3])


def test_busy_503_and_conflict_messages(owner, make_page, stack):
    ctx, pid, _ = owner
    pg = ctx.new_page()
    pg.errors, pg.base = [], stack.url
    pg.route(
        "**/api/projects/*/compounds",
        lambda r: r.fulfill(
            status=503,
            headers={"retry-after": "5", "content-type": "application/json"},
            body='{"detail":"structure processing is busy; try again shortly"}',
        ),
    )
    pg.goto(f"{pg.base}/add.html?p={pid}")
    pg.wait_for_selector("#smi")
    pg.fill("#smi", "CCCCO")
    pg.click("#add-submit")
    pg.wait_for_function("() => document.getElementById('add-msg').dataset.kind === 'error'")
    assert "busy" in pg.inner_text("#add-msg") and "5 seconds" in pg.inner_text("#add-msg")
    pg.unroute("**/api/projects/*/compounds")
    # the real API: a duplicate structure and the 10-compound cap are explained per record, nothing silently fixed
    pg.fill("#smi", "CCO")
    pg.click("#add-submit")
    pg.wait_for_function("() => document.getElementById('add-msg').dataset.kind === 'error'")
    assert "already in this project" in pg.inner_text("#add-msg")
    pg.fill("#smi", "((((")
    pg.click("#add-submit")
    pg.wait_for_selector("#add-problems .tag")
    assert "record 1" in pg.inner_text("#add-problems")
    pg.close()


def test_keyboard_only_sign_in_and_project_table(owner, make_page, stack):
    pg = make_page()
    email = new_email("kbd")
    sign_up(pg, stack, email)
    pg.goto(pg.base + "/auth.html")
    pg.wait_for_selector("#tab-signup")
    pg.focus("#tab-signup")
    pg.keyboard.press("ArrowRight")  # roving tabs: Log in
    assert pg.get_attribute("#tab-login", "aria-selected") == "true"
    pg.keyboard.press("Tab")
    assert pg.evaluate("document.activeElement.id") == "li-email"
    pg.keyboard.type(email)
    pg.keyboard.press("Tab")
    pg.keyboard.type(PW)
    pg.keyboard.press("Enter")
    pg.wait_for_url("**/app.html")
    # project list: keyboard-only create, then open
    pg.wait_for_selector("#np-name")
    pg.focus("#np-name")
    pg.keyboard.type("Keyboard project")
    pg.keyboard.press("Enter")
    pg.wait_for_url("**/app.html?p=*")
    pg.wait_for_selector("#pname")
    assert pg.inner_text("#pname") == "Keyboard project"


def test_project_table_by_keyboard_with_real_rows(owner, stack):
    ctx, pid, _ = owner
    pg = ctx.new_page()
    pg.errors, pg.base = [], stack.url
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    pg.goto(f"{pg.base}/app.html?p={pid}")
    pg.wait_for_selector("tr.sel-row")
    for _ in range(60):
        pg.keyboard.press("Tab")
        if pg.evaluate("document.activeElement.matches('tr.sel-row')"):
            break
    assert pg.evaluate("document.activeElement.matches('tr.sel-row')")
    pg.keyboard.press("Enter")
    assert pg.evaluate("document.activeElement.getAttribute('aria-selected')") == "true"
    pg.keyboard.press("Tab")
    pg.keyboard.press("Enter")  # the row's 3D button
    pg.wait_for_function("() => document.getElementById('viewer').dataset.shown === '1'")
    assert csp_errors(pg) == []
    pg.close()


@pytest.mark.parametrize(
    "name", ["projects", "project", "add", "model", "docking", "account", "report", "auth"]
)
def test_phone_width_has_no_horizontal_scroll_on_real_data(owner, stack, name):
    ctx, pid, _ = owner
    url = {
        "projects": "app.html",
        "project": f"app.html?p={pid}",
        "add": f"add.html?p={pid}",
        "model": "model.html?e=synreg",
        "docking": "docking.html",
        "account": "account.html",
        "report": f"report.html?p={pid}",
        "auth": "auth.html",
    }[name]
    pg = ctx.new_page()
    pg.set_viewport_size({"width": 390, "height": 844})
    pg.goto(f"{stack.url}/{url}")
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(800)
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390
    assert pg.evaluate("document.body.scrollWidth") <= 390
    pg.close()


def test_webgl_missing_falls_back_to_2d_with_a_notice(owner, stack):
    ctx, pid, _ = owner
    for url in (f"app.html?p={pid}", "docking.html"):
        pg = ctx.new_page()
        pg.add_init_script(NO_WEBGL)
        pg.goto(f"{stack.url}/{url}")
        pg.wait_for_selector("[data-testid=webgl-notice]")
        assert pg.locator("canvas").count() == 0 and "2D" in pg.inner_text("[data-testid=webgl-notice]")
        if url.startswith("app"):
            pg.wait_for_selector("tr.sel-row img.mol")
            pg.wait_for_function(
                "() => [...document.querySelectorAll('tr.sel-row img.mol')].every(i => i.complete && i.naturalWidth > 0)",
                timeout=30000,
            )
            pg.locator("tr.sel-row button:has-text('3D')").first.click()
            pg.wait_for_timeout(500)
            assert pg.locator("img#dock2d, #dock2d img").count() == 1
        pg.close()


def test_home_page_layout_and_reduced_motion_under_the_real_csp(make_page):
    pg = make_page(1440, 900)
    pg.goto(pg.base + "/index.html")
    pg.wait_for_selector("#mol3d canvas")
    mol, word = pg.locator("#mol3d").bounding_box(), pg.locator(".word").bounding_box()
    assert mol["x"] > 720 and word["x"] + word["width"] < mol["x"]
    assert pg.locator("#cta").inner_text().strip() == "Sign up / Log in"
    assert csp_errors(pg) == []
    pg.close()
    rm = make_page(800, 600, reduced_motion="reduce")
    rm.goto(rm.base + "/index.html")
    rm.wait_for_selector("#mol3d canvas")
    rm.wait_for_timeout(1200)
    assert (
        rm.get_attribute("#mol3d", "data-spin") == "off" and rm.get_attribute("#mol3d", "data-frames") == "0"
    )
    nw = make_page(800, 600)
    nw.add_init_script(NO_WEBGL)
    nw.goto(nw.base + "/index.html")
    nw.wait_for_selector("#mol3d .fallback img")
    assert "WebGL" in nw.inner_text("#mol3d")


def test_csp_headers_are_applied_per_path_and_ketcher_is_the_only_relaxed_one(make_page, stack):
    pg = make_page()

    def csp(path):
        r = pg.request.get(stack.url + path)
        assert r.status == 200, path
        return r.headers["content-security-policy"]

    for p in (
        "/",
        "/index.html",
        "/app.html",
        "/auth.html",
        "/legal/terms.html",
        "/js/api.js",
        "/css/app.css",
        "/vendor/3Dmol-min.js",
    ):
        c = csp(p)
        assert (
            "style-src 'self';" in c
            and "unsafe-inline" not in c
            and "'unsafe-eval'" not in c
            and "frame-ancestors 'none'" in c
        ), p
        assert ", " not in c.replace("; ", ""), p  # exactly one policy, not two combined
    k = csp("/vendor/ketcher/index.html")
    assert (
        "style-src 'self' 'unsafe-inline'" in k
        and "frame-ancestors 'self'" in k
        and "connect-src 'none'" in k
        and "'unsafe-eval'" not in k
    )
    r = pg.request.get(stack.url + "/app.html")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert pg.request.get(stack.url + "/api/projects").status == 401


def test_draw_structure_with_ketcher_adds_a_compound(owner, stack):
    ctx, pid, _ = owner
    pg = ctx.new_page()
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto(f"{stack.url}/add.html?p={pid}")
    pg.click("#t-draw")
    pg.wait_for_function(
        "() => { const f = document.getElementById('ketcher'); return f.contentWindow && f.contentWindow.ketcher !== undefined; }",
        timeout=60000,
    )
    frame = pg.frame_locator("#ketcher")
    assert frame.locator("body").count() == 1
    pg.evaluate("document.getElementById('ketcher').contentWindow.ketcher.setMolecule('CCCCCCO')")
    pg.wait_for_timeout(500)
    pg.click("#add-submit")
    pg.wait_for_function(
        "() => { const m = document.getElementById('add-msg'); return m.dataset.kind && !m.textContent.startsWith('Sending'); }"
    )
    assert "Added 1 compound" in pg.inner_text("#add-msg"), pg.inner_text("#add-msg")
    assert [e for e in errs if re.search(r"Content Security Policy|Refused to", e, re.I)] == []
    pg.close()


def test_verification_resend_and_password_reset_flow(make_page, stack):
    pg = make_page()
    email = new_email("reset")
    pg.goto(pg.base + "/auth.html")
    pg.wait_for_function("() => document.getElementById('terms-version').textContent.includes('version')")
    pg.fill("#su-email", email)
    pg.fill("#su-pass", PW)
    pg.check("#accept")
    pg.click("#signup-submit")
    pg.wait_for_function(
        "() => document.getElementById('signup-msg').textContent.includes('Check your email')"
    )
    # unverified: login says so and offers to resend
    pg.click("#tab-login")
    pg.fill("#li-email", email)
    pg.fill("#li-pass", PW)
    pg.click("#login-submit")
    pg.wait_for_function("() => document.getElementById('login-msg').dataset.kind === 'error'")
    assert "not verified" in pg.inner_text("#login-msg")
    n = len([1 for k, e, _ in stack.mailer.outbox if k == "verify" and e == email])
    pg.click("#to-resend")
    pg.fill("#rs-email", email)
    pg.click("#resend button[type=submit]")
    pg.wait_for_function(
        "() => document.getElementById('resend-msg').textContent.includes('new verification link')"
    )
    assert len([1 for k, e, _ in stack.mailer.outbox if k == "verify" and e == email]) == n + 1
    pg.goto(f"{pg.base}/auth.html?verify={stack.token('verify', email)}")
    pg.wait_for_function("() => document.getElementById('verify-msg').textContent.includes('is verified')")
    # forgot -> emailed link -> new password -> old password no longer works
    pg.goto(pg.base + "/auth.html?again=1#forgot")
    pg.fill("#fg-email", email)
    pg.click("#forgot button[type=submit]")
    pg.wait_for_function("() => document.getElementById('forgot-msg').textContent.includes('reset link')")
    pg.goto(f"{pg.base}/auth.html?reset={stack.token('reset', email)}")
    new_pw = "another long passphrase 42"
    pg.fill("#rp-pass", new_pw)
    pg.click("#reset button[type=submit]")
    pg.wait_for_function(
        "() => document.getElementById('reset-msg').textContent.includes('Password changed')"
    )
    pg.goto(pg.base + "/auth.html")
    pg.click("#tab-login")
    pg.fill("#li-email", email)
    pg.fill("#li-pass", PW)
    pg.click("#login-submit")
    pg.wait_for_function("() => document.getElementById('login-msg').dataset.kind === 'error'")
    assert "not correct" in pg.inner_text("#login-msg")
    log_in(pg, email, new_pw)
    assert pg.url.endswith("app.html")


def test_changed_terms_must_be_reaccepted(make_page, stack):
    pg = make_page()
    account(pg, stack, "terms")
    old = stack.app.state.accounts_config
    stack.app.state.accounts_config = AccountsConfig(**{**old.model_dump(), "terms_version": "2027-01"})
    try:
        pg.goto(pg.base + "/app.html")
        pg.wait_for_url("**/auth.html?*why=terms*")
        pg.wait_for_selector("#p-terms:not([hidden])")
        assert "2027-01" in pg.inner_text("#reaccept-version") and pg.is_disabled("#reaccept-submit")
        pg.check("#reaccept-box")
        pg.click("#reaccept-submit")
        pg.wait_for_url(lambda u: "auth.html" not in u)
        pg.wait_for_selector("#np-name")
        assert stack.sql("SELECT count(*) FROM terms_acceptance WHERE version = '2027-01'")[0][0] >= 3
    finally:
        stack.app.state.accounts_config = old
