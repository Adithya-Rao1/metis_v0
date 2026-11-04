"""Writes small screenshots of every screen to tests/web/screenshots/ (dark scheme, test-only mock API data) and checks they were produced."""
from pathlib import Path

import pytest

OUT = Path(__file__).resolve().parents[2] / "tests" / "web" / "screenshots"
PID = "aaaaaaaa-0000-4000-8000-000000000001"
SHOTS = [
    ("00_home", "index.html", 1440, 900, "#mol3d canvas"),
    ("00_home_mobile", "index.html", 390, 844, "#mol3d canvas"),
    ("01_signup", "auth.html", 1000, 760, "#signup"),
    ("02_project_table", f"app.html?p={PID}", 1440, 1000, "#viewer canvas"),
    ("03_add_compounds", f"add.html?p={PID}", 1280, 900, "#check .tag"),
    ("04_uncertainty_panel", f"compound.html?p={PID}&c=cccccccc-0000-4000-8000-000000000003&e=logd", 1440, 900, "[data-testid=panel-value]"),
    ("05_model_data", "model.html?e=logd", 1280, 1000, "#coverage-table"),
    ("06_docking_prep", "docking.html", 1440, 900, "#viewer canvas"),
    ("07_account_consents", "account.html", 1100, 700, "[data-consent]"),
    ("09_export_report", f"report.html?p={PID}", 1280, 900, "#csv-prev tbody tr"),
    ("08_project_table_mobile", f"app.html?p={PID}", 390, 844, "tr.sel-row"),
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
