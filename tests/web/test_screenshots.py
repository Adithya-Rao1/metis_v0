"""Writes small screenshots of every screen to tests/web/screenshots/ (dark scheme) and checks they were produced."""
from pathlib import Path

import pytest

OUT = Path(__file__).resolve().parents[2] / "tests" / "web" / "screenshots"
SHOTS = [
    ("00_home", "index.html", 1440, 900, "#mol3d canvas"),
    ("00_home_mobile", "index.html", 390, 844, "#mol3d canvas"),
    ("01_signup", "auth.html", 1000, 760, "#signup"),
    ("02_project_table", "app.html", 1440, 1000, "#viewer canvas"),
    ("03_add_compounds", "add.html", 1280, 900, "#check .tag"),
    ("04_uncertainty_panel", "compound.html?c=3&e=herg", 1440, 900, "#viewer canvas"),
    ("05_model_data", "model.html?e=herg", 1280, 1000, "#viewer canvas"),
    ("06_docking_prep", "docking.html", 1440, 900, "#viewer canvas"),
    ("07_account_consents", "account.html", 1100, 700, "[data-consent]"),
    ("09_export_report", "report.html", 1280, 900, "#rp-foot"),
    ("08_project_table_mobile", "app.html", 390, 844, "tr.sel-row"),
]


@pytest.mark.parametrize("name,url,w,h,ready", SHOTS)
def test_screenshot(make_page, name, url, w, h, ready):
    OUT.mkdir(parents=True, exist_ok=True)
    pg = make_page(w, h, color_scheme="dark")
    pg.goto(pg.base + "/" + url)
    pg.wait_for_selector(ready)
    pg.wait_for_timeout(1500)
    path = OUT / f"{name}.png"
    pg.screenshot(path=str(path))
    assert path.stat().st_size > 2000
