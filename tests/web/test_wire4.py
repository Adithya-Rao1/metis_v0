"""Wave 4 wiring against the MOCK API (tests/web/mockapi.py): account email, rename, deletes with confirm dialogs, SMILES pictures,
model-card direction, and the terms re-accept answer on GETs. The real routes are exercised by the backend's own tests."""
from mockapi import MEAS_ID, PID, MockApi

PROJECT = f"app.html?p={PID}"
COMPOUND = f"compound.html?p={PID}&c=cccccccc-0000-4000-8000-000000000003&e=logd"


def calls(pg, method, suffix):
    return [c for c in pg.api.calls if c[0] == method and c[1].endswith(suffix)]


def test_account_page_shows_the_email(make_page):
    pg = make_page()
    pg.goto(pg.base + "/account.html")
    pg.wait_for_selector("#acct-frame:not([hidden])")
    assert pg.inner_text("#acct-email") == "analyst@example.org"


def test_model_page_and_ranking_default_use_higher_is_better(make_page):
    pg = make_page()
    pg.goto(pg.base + "/model.html?e=logd")
    pg.wait_for_selector("[data-testid=direction]")
    assert "lower" in pg.inner_text("[data-testid=direction]")
    pg.goto(pg.base + "/model.html?e=ames")
    pg.wait_for_selector("#datasets")
    assert pg.locator("[data-testid=direction]").count() == 0
    pg.goto(pg.base + "/" + PROJECT)
    pg.wait_for_selector("[data-direction=logd]")
    assert pg.eval_on_selector("[data-direction=logd]", "e => e.value") == "lower"
    assert pg.eval_on_selector("[data-direction=ames]", "e => e.value") == "higher"


def test_rename_project_from_the_project_page(make_page):
    pg = make_page()
    pg.goto(pg.base + "/" + PROJECT)
    pg.wait_for_selector("#rename-project")
    pg.click("#rename-project")
    pg.fill("#cd-name", "Renamed series")
    pg.click("#cd-cancel")
    assert calls(pg, "PATCH", PID) == []
    pg.click("#rename-project")
    pg.fill("#cd-name", "Renamed series")
    pg.click("#cd-ok")
    pg.wait_for_function("() => document.getElementById('pname').textContent === 'Renamed series'")
    assert calls(pg, "PATCH", PID)[-1][2] == {"name": "Renamed series"}
    assert "Renamed series" in pg.title()


def test_rename_project_from_the_list(make_page):
    pg = make_page()
    pg.goto(pg.base + "/app.html")
    pg.click("button:has-text('Rename')")
    pg.fill("#cd-name", "From list")
    pg.click("#cd-ok")
    pg.wait_for_function("() => document.querySelector('.proj-link') && document.querySelector('.proj-link').textContent === 'From list'")
    assert calls(pg, "PATCH", PID)[-1][2] == {"name": "From list"}


def test_delete_compound_asks_first(make_page):
    pg = make_page()
    pg.goto(pg.base + "/" + PROJECT)
    sel = "[data-delete-compound='cccccccc-0000-4000-8000-000000000001']"
    pg.wait_for_selector(sel)
    pg.click(sel)
    pg.click("#cd-cancel")
    assert [c for c in pg.api.calls if c[0] == "DELETE"] == []
    pg.click(sel)
    pg.click("#cd-ok")
    pg.wait_for_function("() => !document.querySelector(\"[data-delete-compound='cccccccc-0000-4000-8000-000000000001']\")")
    assert calls(pg, "DELETE", "/compounds/cccccccc-0000-4000-8000-000000000001")


def test_delete_measurement_asks_first(make_page):
    pg = make_page(api=MockApi(measured=True))
    pg.goto(pg.base + "/" + PROJECT)
    sel = f"[data-delete-measurement='{MEAS_ID}']"
    pg.wait_for_selector(sel)
    pg.click(sel)
    pg.click("#cd-cancel")
    assert calls(pg, "DELETE", MEAS_ID) == []
    pg.click(sel)
    pg.click("#cd-ok")
    pg.wait_for_function("(s) => !document.querySelector(s)", arg=sel)
    assert calls(pg, "DELETE", MEAS_ID)


def test_terms_reaccept_on_a_get_sends_the_user_to_sign_in(make_page):
    pg = make_page()
    pg.api.overrides[("GET", rf"^/projects/{PID}$")] = (403, {"detail": "terms_reaccept_required"}, {})
    pg.goto(pg.base + "/" + PROJECT)
    pg.wait_for_url("**/auth.html?**")
    assert "why=terms" in pg.url


def test_docking_typed_smiles_gets_a_picture_when_the_route_exists(make_page):
    pg = make_page()
    pg.goto(pg.base + "/docking.html")
    pg.fill("#lig-smiles", "CCO")
    pg.press("#lig-smiles", "Tab")
    pg.wait_for_selector("#lig2d img")
    assert calls(pg, "POST", "/dockprep/depiction")[-1][2] == {"smiles": "CCO"}


def test_docking_without_the_route_shows_no_picture_and_no_error(make_page):
    pg = make_page(api=MockApi(depiction_route=False))
    pg.goto(pg.base + "/docking.html")
    pg.fill("#lig-smiles", "CCO")
    pg.press("#lig-smiles", "Tab")
    pg.wait_for_timeout(500)
    assert pg.locator("#lig2d img").count() == 0
    assert not [e for e in pg.errors if "404" not in e]


def test_nearest_training_pictures_follow_the_route(make_page):
    pg = make_page()
    pg.goto(pg.base + "/" + COMPOUND)
    pg.wait_for_selector("#nearest [data-nearest] img")
    pg2 = make_page(api=MockApi(depiction_route=False))
    pg2.goto(pg2.base + "/" + COMPOUND)
    pg2.wait_for_selector("#nearest [data-nearest]")
    pg2.wait_for_timeout(400)
    assert pg2.locator("#nearest img").count() == 0
