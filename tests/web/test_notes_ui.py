"""Notes on the compound page: add, edit, delete (with a confirmation), hostile text stays text."""
import re

from mockapi import PID, MockApi
from test_pages import COMPOUND

HOSTILE = '<img src=x onerror="window.__xss=1"><script>window.__xss=1</script>'


def _open(make_page, api=None):
    api = api or MockApi()
    pg = make_page(1280, 900, api=api)
    pg.goto(pg.base + "/" + COMPOUND)
    pg.wait_for_selector("#note-form")
    pg.wait_for_selector("#notes-empty, [data-note]")
    return pg, api


def test_add_edit_delete_a_note_with_confirmation(make_page):
    pg, api = _open(make_page)
    assert "No notes yet" in pg.inner_text("#notes")
    pg.fill("#note-new", "  first thought\nsecond line  ")
    pg.click("#note-add")
    pg.wait_for_selector("[data-note]")
    assert [c for c in api.calls if c[0] == "POST" and c[1].endswith("/notes")][0][2] == {"body": "first thought\nsecond line"}
    assert pg.inner_text("[data-note-text]") == "first thought\nsecond line" and pg.input_value("#note-new") == ""
    # edit
    pg.click("[data-note] button:has-text('Edit')")
    pg.fill("#note-edit-0", "revised")
    pg.click(".note-edit button:has-text('Save')")
    pg.wait_for_function("() => document.querySelector('[data-note-text]')?.textContent === 'revised'")
    assert api.notes[0]["body"] == "revised"
    # cancel keeps the text
    pg.click("[data-note] button:has-text('Edit')")
    pg.fill("#note-edit-0", "discarded")
    pg.click(".note-edit button:has-text('Cancel')")
    assert pg.inner_text("[data-note-text]") == "revised" and api.notes[0]["body"] == "revised"
    # delete: cancel first, nothing is sent; then confirm
    pg.click("[data-note] button:has-text('Delete')")
    pg.wait_for_selector("dialog[open]")
    assert "cannot be undone" in pg.inner_text("dialog")
    pg.click("#cd-cancel")
    assert len(api.notes) == 1 and not [c for c in api.calls if c[0] == "DELETE" and "/notes/" in c[1]]
    pg.click("[data-note] button:has-text('Delete')")
    pg.click("#cd-ok")
    pg.wait_for_selector("#notes-empty")
    assert api.notes == [] and [c for c in api.calls if c[0] == "DELETE" and "/notes/" in c[1]]


def test_empty_note_is_not_sent_and_server_errors_are_shown(make_page):
    pg, api = _open(make_page)
    pg.fill("#note-new", "   ")
    pg.click("#note-add")
    assert "Write something" in pg.inner_text("#note-msg")
    assert not [c for c in api.calls if c[0] == "POST" and c[1].endswith("/notes")]
    api.overrides[("POST", r"/notes$")] = (409, {"detail": "note limit reached"}, {})
    pg.fill("#note-new", "x")
    pg.click("#note-add")
    pg.wait_for_function("() => document.getElementById('note-msg').dataset.kind === 'error'")
    assert pg.input_value("#note-new") == "x" and pg.locator("#note-add").is_enabled()


def test_hostile_note_text_is_rendered_as_text(make_page):
    api = MockApi()
    api.notes = [{"id": "cccccccc-0000-4000-8000-0000000000f1", "body": HOSTILE, "created_at": "2026-01-02T00:00:00Z"}]
    pg, _ = _open(make_page, api)
    assert pg.inner_text("[data-note-text]") == HOSTILE
    assert pg.locator("[data-note] img, [data-note] script").count() == 0
    pg.click("[data-note] button:has-text('Edit')")
    assert pg.input_value("#note-edit-0") == HOSTILE
    pg.fill("#note-new", HOSTILE)
    pg.click(".note-edit button:has-text('Cancel')")
    pg.click("#note-add")
    pg.wait_for_function("() => document.querySelectorAll('[data-note]').length === 2")
    assert pg.evaluate("window.__xss") is None
    assert [t for t in pg.eval_on_selector_all("[data-note-text]", "e=>e.map(x=>x.textContent)")] == [HOSTILE, HOSTILE]


def test_notes_load_failure_offers_retry(make_page):
    api = MockApi()
    api.overrides[("GET", r"/notes$")] = (500, {"detail": "boom"}, {})
    pg = make_page(1280, 900, api=api)
    pg.goto(pg.base + "/" + COMPOUND)
    pg.wait_for_selector("#notes [role=alert] button:has-text('Try again')")
    del api.overrides[("GET", r"/notes$")]
    pg.click("#notes button:has-text('Try again')")
    pg.wait_for_selector("#notes-empty")


def test_note_controls_have_accessible_names_and_no_new_headings_skip(make_page):
    api = MockApi()
    api.notes = [{"id": "cccccccc-0000-4000-8000-0000000000f2", "body": "a", "created_at": "2026-01-02T00:00:00Z"}]
    pg, _ = _open(make_page, api)
    for sel in ("#note-new", "[data-note] button"):
        for h in pg.query_selector_all(sel):
            assert (h.get_attribute("aria-label") or h.inner_text().strip() or pg.evaluate("e=>e.labels&&e.labels.length", h))
    hs = pg.evaluate("[...document.querySelectorAll('h1,h2,h3,h4')].map(h => +h.tagName[1])")
    assert all(b - a <= 1 for a, b in zip(hs, hs[1:])), hs
