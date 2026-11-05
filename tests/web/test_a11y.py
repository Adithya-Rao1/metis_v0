"""Wave 4 accessibility pass: zero serious/critical findings on every page (including Lead Studio), light and dark, and no horizontal
scroll at phone width. The audit is tests/web/a11y.py (axe-core's most common serious/critical rules, run in the page)."""
import re

import pytest
from a11y import audit
from mockapi import PID, LeadMockApi, install_lead
from test_pages import COMPOUND, PROJECT

PAGES = ["index.html", "auth.html", "app.html", PROJECT, f"add.html?p={PID}", COMPOUND, "model.html?e=logd", "docking.html", "account.html", f"report.html?p={PID}",
         "legal/terms.html", "legal/privacy.html", "legal/disclaimer.html"]
NOISE = ("GL Driver", "GPU stall", "WebGL", "swiftshader", "SwiftShader")


def settle(pg):
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(500)


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("page_url", PAGES)
def test_no_serious_or_critical_issues(make_page, page_url, scheme):
    pg = make_page(1280, 800, color_scheme=scheme)
    pg.goto(pg.base + "/" + page_url)
    settle(pg)
    assert audit(pg) == []


@pytest.mark.parametrize("page_url", ["add.html?p=" + PID, "app.html?p=" + PID, "docking.html"])
def test_hidden_tabs_and_dialogs_are_clean_too(make_page, page_url):
    pg = make_page(1280, 800)
    pg.goto(pg.base + "/" + page_url)
    settle(pg)
    for t in pg.query_selector_all("[role=tab]"):
        t.click()
        assert audit(pg) == [], t.get_attribute("id")
    if page_url.startswith("app.html?p="):
        pg.click("#rename-project")
        assert audit(pg) == []
        pg.click("#cd-cancel")


def _lead(make_page, width, scheme="dark"):
    pg = make_page(width, 844 if width < 600 else 800, color_scheme=scheme)
    pg.lead = LeadMockApi()
    install_lead(pg, pg.lead)
    pg.goto(pg.base + "/lead/index.html")
    pg.wait_for_function("() => document.querySelectorAll('#sel-target option').length > 0 && !document.getElementById('target-body').hidden", timeout=20000)
    pg.wait_for_timeout(500)
    return pg


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_lead_studio_has_no_serious_or_critical_issues(make_page, scheme):
    pg = _lead(make_page, 1280, scheme)
    assert audit(pg) == []
    for key in ("w", "e", "r", "q"):
        pg.keyboard.press(key)
        pg.wait_for_timeout(500)
        assert audit(pg) == [], key


@pytest.mark.parametrize("page_url", PAGES)
def test_phone_width_has_no_horizontal_scroll(make_page, page_url):
    pg = make_page(360, 740)
    pg.goto(pg.base + "/" + page_url)
    settle(pg)
    assert pg.evaluate("document.documentElement.scrollWidth") <= 360, page_url
    assert pg.evaluate("document.body.scrollWidth") <= 360, page_url


def test_lead_studio_phone_width_has_no_horizontal_scroll(make_page):
    pg = _lead(make_page, 360)
    assert pg.evaluate("document.documentElement.scrollWidth") <= 360
    assert pg.evaluate("document.body.scrollWidth") <= 360
    for key in ("w", "e", "r"):
        pg.keyboard.press(key)
        pg.wait_for_timeout(400)
        assert pg.evaluate("document.documentElement.scrollWidth") <= 360, key
