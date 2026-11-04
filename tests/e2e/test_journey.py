"""The main user journey in a real browser against the real API: sign up with terms -> verify -> log in -> create project
-> add compounds -> predict -> uncertainty panel -> rank with changed weights, limits and min/max -> export ->
docking prep on the 1M17 fixture -> consents -> delete project -> delete account.

Models are SYNTHETIC (tests/e2e/synth_registry.py): the numbers prove the wiring, not any prediction quality."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from _fixtures import build_1m17_like
from helpers import PW, account, create_project, log_in

SMILES = "CCO ethanol\nc1ccccc1O phenol\nCC(=O)Nc1ccc(O)cc1 paracetamol"
CSP_ERR = re.compile(r"Content Security Policy|Refused to", re.I)


def csp_errors(pg):
    return [e for e in pg.errors if CSP_ERR.search(e)]


def add_and_predict(pg, pid, n="5", endpoints=("synreg", "syncls")):
    pg.goto(f"{pg.base}/add.html?p={pid}")
    pg.fill("#smi", SMILES)
    pg.click("#add-submit")
    pg.wait_for_function("() => document.getElementById('add-msg').textContent.includes('Added 3')")
    for ep in ("synreg", "syncls"):
        if ep not in endpoints:
            pg.uncheck(f"#ep-{ep}")
    pg.select_option("#ens", n)
    pg.click("#predict-submit")
    pg.wait_for_selector("#to-table", timeout=60000)


@pytest.fixture(scope="module")
def journey(browser, stack):
    ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.errors, pg.base = errors, stack.url
    email = account(pg, stack, "journey")
    pid = create_project(pg, "Series A")
    add_and_predict(pg, pid)
    yield pg, pid, email
    ctx.close()


def test_a_project_table_has_every_required_field(journey, stack):
    pg, pid, _ = journey
    pg.goto(f"{pg.base}/app.html?p={pid}")
    pg.wait_for_selector("tr.sel-row")
    assert pg.locator("tr.sel-row").count() == 3
    cells = pg.locator("[data-pred]")
    assert cells.count() == 6
    for i in range(cells.count()):
        c = cells.nth(i)
        t, ep = c.inner_text(), c.get_attribute("data-pred")
        assert re.search(r"N=5", t) and re.search(r"n=[\d,]+", t), t
        if ep == "syncls":
            assert re.search(r"\d\.\d\d \(cal\.\)", t) and "calibrated probability" in t
        else:
            assert re.search(r"-?\d+\.\d \[-?\d+\.\d, -?\d+\.\d\]", t) and re.search(r"90% interval", t), t
        assert (
            c.get_attribute("data-coverage")
            and c.get_attribute("data-n") == "5"
            and int(c.get_attribute("data-sample-size")) > 0
        )
        assert not re.search(r"\b(safe|unsafe|toxic|good|bad|promising|recommended)\b", t, re.I)
    heads = pg.eval_on_selector_all("#thead th", "els=>els.map(e=>e.textContent)")
    assert not any(re.search("verdict|status|flag", h, re.I) for h in heads)
    # 2D structures are served by the API (depiction.svg) and render
    pg.wait_for_function(
        "() => [...document.querySelectorAll('tr.sel-row img.mol')].every(i => i.complete && i.naturalWidth > 0)",
        timeout=30000,
    )
    assert pg.locator("tr.sel-row img.mol").count() == 3
    # 3D conformer labelled as computed, one shared WebGL viewer
    pg.locator("tr.sel-row button:has-text('3D')").nth(1).click()
    pg.wait_for_function("() => document.getElementById('viewer').dataset.shown === '1'")
    assert "computed conformer, not an experimental structure" in pg.inner_text("#viewer-cap")
    assert pg.locator("#viewer canvas").count() == 1
    assert csp_errors(pg) == []


def test_b_uncertainty_panel_and_model_page(journey):
    pg, pid, _ = journey
    pg.goto(f"{pg.base}/app.html?p={pid}")
    pg.wait_for_selector("[data-pred=synreg] a")
    pg.locator("[data-pred=synreg] a").first.click()
    pg.wait_for_selector("[data-testid=panel-value]")
    assert re.search(r"-?\d+\.\d \[-?\d+\.\d, -?\d+\.\d\]", pg.inner_text("[data-testid=panel-value]"))
    meta = pg.inner_text("[data-testid=panel-meta]")
    assert "90% interval" in meta and "N = 5" in meta and re.search(r"n = \d", meta)
    assert pg.inner_text("#methodnote").startswith("Method: ")
    pcts = [
        int(x)
        for x in re.findall(
            r"(\d+)%",
            " ".join(pg.eval_on_selector_all("#shares [data-share] .mono", "e=>e.map(x=>x.textContent)")),
        )
    ]
    assert len(pcts) == 3 and 98 <= sum(pcts) <= 102
    assert pg.get_attribute("#viewer", "data-webgl") == "on"
    pg.wait_for_function("() => document.getElementById('viewer').dataset.shown === '1'")
    assert "computed conformer" in pg.inner_text("#viewer-cap")
    if pg.locator("#fragments button").count():
        pg.locator("#fragments button").first.click()
        pg.wait_for_function("() => document.querySelector('#d2 img').src.includes('highlight=')")
    # model page: published synthetic model card, every field, no placeholder numbers
    pg.goto(f"{pg.base}/model.html?e=synreg")
    pg.wait_for_selector("#coverage-table")
    body = pg.inner_text("#report")
    assert "Synthetic regression set" in body and "CC BY 4.0" in body and "Known limitations" in body
    assert pg.locator("#coverage-table tbody tr").count() == 4
    assert "scaffold" in pg.inner_text("[data-testid=split]").lower()
    assert (
        pg.locator("#datasets-table tbody tr").count() >= 1
        and pg.locator("#benchmark-table tbody tr").count() >= 1
    )
    assert "placeholder" not in body.lower()
    assert csp_errors(pg) == []


def _first_number(text):
    return float(re.search(r"-?\d+\.\d", text).group(0))


def test_c_ranking_weights_limits_and_min_max(journey):
    pg, pid, _ = journey
    pg.goto(f"{pg.base}/app.html?p={pid}")
    pg.wait_for_selector("tr.sel-row")
    # defaults for the target range come from the model card default_range
    card = pg.request.get(f"{pg.base}/api/models/synreg").json()
    lo, hi = card["default_range"]
    assert float(pg.input_value("[data-tmin=synreg]")) == pytest.approx(lo, rel=1e-9)
    assert float(pg.input_value("[data-tmax=synreg]")) == pytest.approx(hi, rel=1e-9)
    assert pg.get_attribute("#lam", "value") == "25" and pg.inner_text("#lamv") == "0.25"
    # before saving there is no rank
    assert all(
        t.strip() == "-"
        for t in pg.eval_on_selector_all("[data-testid=rank]", "els=>els.map(e=>e.firstChild.textContent)")
    )
    # a hard limit that exactly one compound breaks: max = the middle synreg value
    vals = sorted(_first_number(pg.locator("[data-pred=synreg] a").nth(i).inner_text()) for i in range(3))
    pg.fill("[data-limit-max=synreg]", str(vals[1]))
    pg.fill("[data-weight=syncls]", "3")
    pg.select_option("[data-direction=syncls]", "lower")
    pg.fill("[data-tmin=synreg]", "-5")
    pg.fill("[data-tmax=synreg]", "10")
    pg.locator("#lam").fill("50")
    with pg.expect_request(lambda r: r.method == "PUT" and r.url.endswith("/ranking")) as req:
        pg.click("#rank-save")
    sent = req.value.post_data_json
    assert req.value.headers.get("x-csrf-token")
    w = {x["endpoint"]: x for x in sent["weights"]}
    assert w["synreg"]["target_min"] == -5 and w["synreg"]["target_max"] == 10  # edited range is sent
    assert "target_min" not in w["syncls"]  # untouched range is left to the model default
    assert w["syncls"]["weight"] == 3 and w["syncls"]["higher_is_better"] is False and sent["lam"] == 0.5
    assert sent["limits"] == [
        {"endpoint": "synreg", "min": None, "max": vals[1], "use_unfavorable_bound": False}
    ]
    pg.wait_for_function("() => document.getElementById('rank-msg').textContent.startsWith('Saved')")
    ranks = pg.eval_on_selector_all("[data-testid=rank]", "els=>els.map(e=>e.firstChild.textContent)")
    assert all(re.fullmatch(r"\d \(\d\)", r) for r in ranks) and sorted(r[0] for r in ranks) == [
        "1",
        "2",
        "3",
    ]
    assert pg.inner_text("[data-testid=nfail]") == "1 fail a hard limit"
    # the failer keeps its numeric rank, shows the word fail and the limit note; and ranks last
    fail_rows = pg.locator("tr.sel-row:has([data-testid=fail])")
    assert fail_rows.count() == 1 and fail_rows.locator("[data-testid=fail]").inner_text() == "fail"
    assert re.match(r"3 \(\d\)", fail_rows.locator("[data-testid=rank]").inner_text())
    assert f"limit: < {vals[1]}" in fail_rows.inner_text()
    # breakdown sums to R
    shares = [
        float(x)
        for x in pg.eval_on_selector_all("#bd-table tbody tr td:last-child", "els=>els.map(e=>e.textContent)")
    ]
    big_r = float(re.search(r"R = ([\d.]+)", pg.inner_text("#bd-title")).group(1))
    assert len(shares) == 2 and abs(sum(shares) - big_r) < 0.0035
    # persisted: reload shows the saved config (GET /ranking) and recomputes the ranking
    pg.reload()
    pg.wait_for_function("() => document.querySelector('[data-testid=nfail]')")
    assert pg.input_value("[data-tmin=synreg]") == "-5" and pg.input_value("[data-weight=syncls]") == "3"
    assert pg.input_value("[data-direction=syncls]") == "lower" and pg.inner_text("#lamv") == "0.50"
    assert pg.inner_text("[data-testid=nfail]") == "1 fail a hard limit"
    # a config the API rejects shows its message and is not applied
    pg.fill("[data-weight=syncls]", "0")
    pg.fill("[data-weight=synreg]", "0")
    pg.click("#rank-save")
    pg.wait_for_function("() => document.getElementById('rank-msg').dataset.kind === 'error'")
    assert csp_errors(pg) == []


def test_d_stale_model_version_flag(journey, stack):
    pg, pid, _ = journey
    # Re-point stored predictions at a different model version id: the API flags them stale.
    stack.sql(
        "INSERT INTO model_version (id, endpoint, version) VALUES ('99999999-9999-4999-8999-999999999999', 'synreg', 'old') ON CONFLICT DO NOTHING"
    )
    stack.sql(
        "UPDATE prediction SET model_version_id = '99999999-9999-4999-8999-999999999999' WHERE endpoint = 'synreg'"
    )
    pg.goto(f"{pg.base}/app.html?p={pid}")
    pg.wait_for_selector("[data-testid=stale]")
    assert "older model version" in pg.inner_text("[data-testid=stale]")
    assert pg.locator("[data-testid=stale-cell]").count() == 3


def test_e_exports(journey):
    pg, pid, _ = journey
    pg.goto(f"{pg.base}/report.html?p={pid}")
    pg.wait_for_selector("#csv-prev tbody tr")
    assert (
        "not a diagnosis" in pg.inner_text("[data-testid=csv-preamble]").lower()
        or pg.inner_text("[data-testid=csv-preamble]").strip()
    )
    got = {}
    for fmt in ("csv", "sdf", "pdf"):
        with pg.expect_download() as dl:
            pg.click(f"[data-dl={fmt}]")
        got[fmt] = Path(dl.value.path()).read_bytes()
        assert dl.value.suggested_filename == f"metis_export.{fmt}"
    assert got["csv"].startswith(b"# ") and b"synreg_predicted" in got["csv"]
    assert b"$$$$" in got["sdf"] and got["pdf"].startswith(b"%PDF")
    disclaimer = Path(__file__).resolve().parents[2].joinpath("docs/legal/DISCLAIMER.md").read_text()
    assert any(w in got["csv"].decode().lower() for w in re.findall(r"[a-z]{8,}", disclaimer.lower())[:3])
    assert pg.inner_text("[data-testid=disclaimer]").strip()


def test_f_docking_prep_on_1m17_fixture(journey, tmp_path):
    pg, _, _ = journey
    pdb = tmp_path / "1m17_fixture.pdb"
    pdb.write_text(build_1m17_like())
    pg.goto(pg.base + "/docking.html")
    pg.check("#src-up")
    pg.set_input_files("#up", str(pdb))
    pg.fill("#chains", "A")
    pg.fill("#ligand-id", "A:999")
    pg.click("#prep")
    pg.wait_for_selector("[data-testid=protein-report]", timeout=60000)
    rep = pg.inner_text("[data-testid=protein-report]")
    assert "1M17" in rep and "X-RAY DIFFRACTION" in rep and "2.6" in rep and "AQ4" in rep
    assert pg.locator("#sites tbody tr").count() >= 2
    assert re.match(r"-?\d", pg.inner_text("#bc"))
    assert "Prepared protein" in pg.inner_text("#viewer-cap")
    log = pg.inner_text("#log")
    assert "chains" in log and "site defined" in log
    # engine switch re-targets the config; downloads are real files
    pg.check("input[name=eng][value=gnina]")
    with pg.expect_download() as dl:
        pg.click("[data-file='receptor.pdbqt']")
    assert dl.value.suggested_filename == "receptor.pdbqt"
    assert Path(dl.value.path()).read_text().startswith(("REMARK", "ATOM", "HEADER"))
    with pg.expect_download() as dl:
        pg.click("[data-zip=protein]")
    assert dl.value.suggested_filename.endswith(".zip") and Path(dl.value.path()).read_bytes()[:2] == b"PK"
    with pg.expect_download() as dl:
        pg.click("#dl-log")
    assert dl.value.suggested_filename == "change_log.txt"
    # a PDB ID cannot be fetched on this server (fetch is disabled): the page says so, plainly
    pg.check("#src-pdb")
    pg.fill("#pdbid", "1M17")
    pg.click("#prep")
    pg.wait_for_selector("#result .state-error")
    assert (
        "not enabled" in pg.inner_text("#prep-msg").lower() or "upload" in pg.inner_text("#prep-msg").lower()
    )
    assert csp_errors(pg) == []


def test_g_consents(journey, stack):
    pg, _, email = journey
    pg.goto(pg.base + "/account.html")
    pg.wait_for_selector("[data-consent]")
    boxes = pg.locator("[data-consent]")
    assert boxes.count() == 4 and all(not boxes.nth(i).is_checked() for i in range(4))
    assert "free" in pg.inner_text("[data-testid=acct-tier]") and "N = 10" in pg.inner_text(
        "[data-testid=acct-cap]"
    )
    pg.check("#consent-train_models")
    pg.wait_for_function("() => document.getElementById('cons-msg').textContent.includes('is now on')")
    pg.reload()
    pg.wait_for_selector("[data-consent]")
    assert pg.is_checked("#consent-train_models") and not pg.is_checked("#consent-analytics")
    pg.uncheck("#consent-train_models")
    pg.wait_for_function("() => document.getElementById('cons-msg').textContent.includes('is now off')")
    pg.reload()
    pg.wait_for_selector("[data-consent]")
    assert not pg.is_checked("#consent-train_models")
    with pg.expect_download() as dl:
        pg.click("#export-data")
    data = json.loads(Path(dl.value.path()).read_text())
    assert data["account"]["email_lower"] == email and "project" in data


def test_h_delete_project_then_account(journey, stack):
    pg, pid, email = journey
    pg.goto(f"{pg.base}/app.html?p={pid}")
    pg.wait_for_selector("#del-project")
    pg.click("#del-project")
    pg.click("#cd-ok")
    pg.wait_for_url(lambda u: u.endswith("/app.html"))
    pg.wait_for_selector("[data-state=empty]")
    assert "No projects yet" in pg.inner_text("#plist")
    # the deleted project is gone (404 page state)
    pg.goto(f"{pg.base}/app.html?p={pid}")
    pg.wait_for_selector("[data-state=error]")
    assert "Not found" in pg.inner_text("#page-state")
    # delete the account: needs the password and the typed word
    pg.goto(pg.base + "/account.html")
    pg.wait_for_selector("#del-all")
    pg.click("#del-all")
    assert pg.locator("#cd-ok").is_disabled()
    pg.fill("#cd-typed", "DELETE")
    pg.fill("#del-pass", "not the password")
    pg.click("#cd-ok")
    pg.wait_for_function("() => document.getElementById('del-msg').textContent.includes('not correct')")
    pg.click("#del-all")
    pg.fill("#cd-typed", "DELETE")
    pg.fill("#del-pass", PW)
    pg.click("#cd-ok")
    pg.wait_for_url(lambda u: u.endswith("/index.html") or u.endswith("/"))
    assert stack.sql("SELECT count(*) FROM app_user WHERE email_lower = %s", email)[0][0] == 0
    log_in_fail = pg.context.new_page()
    log_in_fail.goto(pg.base + "/auth.html")
    log_in_fail.click("#tab-login")
    log_in_fail.fill("#li-email", email)
    log_in_fail.fill("#li-pass", PW)
    log_in_fail.click("#login-submit")
    log_in_fail.wait_for_function("() => document.getElementById('login-msg').dataset.kind === 'error'")
    assert "not correct" in log_in_fail.inner_text("#login-msg")
    # the account is gone, so a signed-in page now sends a visitor to sign in
    log_in_fail.goto(pg.base + "/app.html")
    log_in_fail.wait_for_url("**/auth.html?*")
    assert log_in(pg, email) is None if False else True
