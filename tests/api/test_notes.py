"""Project notes: create, list, edit, delete. Owner only (404 for everyone else), size caps, text never audited."""

import uuid

import pytest
from api_support import H

from backend.projects.routes import MAX_NOTE_CHARS, MAX_NOTES_PER_PROJECT

HOSTILE = ['<script>alert(1)</script>', "'; DROP TABLE note; --", "<img src=x onerror=alert(1)>", "line1\nline2\ttabbed", "‮RTL‬ \U0001f9ea ünï", "{{7*7}} ${7*7} %s %(x)s"]


def _mk(client, pid, text="first note", who="a"):
    r = client.post(f"/projects/{pid}/notes", json={"body": text}, headers=H(who))
    assert r.status_code == 201, r.text
    return r.json()


def test_create_list_update_delete_roundtrip(client, project, su):
    n = _mk(client, project, "  first note  ")
    assert n["body"] == "first note" and set(n) == {"id", "body", "created_at"}
    _mk(client, project, "second")
    listed = client.get(f"/projects/{project}/notes", headers=H("a")).json()
    assert [x["body"] for x in listed["notes"]] == ["first note", "second"]
    assert listed["max_chars"] == MAX_NOTE_CHARS and listed["max_notes"] == MAX_NOTES_PER_PROJECT
    r = client.patch(f"/projects/{project}/notes/{n['id']}", json={"body": "edited"}, headers=H("a"))
    assert r.status_code == 200 and r.json()["body"] == "edited" and r.json()["id"] == n["id"]
    assert [x["body"] for x in client.get(f"/projects/{project}/notes", headers=H("a")).json()["notes"]] == ["edited", "second"]
    assert client.delete(f"/projects/{project}/notes/{n['id']}", headers=H("a")).status_code == 204
    assert [x["body"] for x in client.get(f"/projects/{project}/notes", headers=H("a")).json()["notes"]] == ["second"]


def test_other_users_get_the_same_404_for_every_note_route(client, project, su):
    n = _mk(client, project, "private thought")
    miss = uuid.uuid4()
    for method, url, body in (
        ("GET", f"/projects/{project}/notes", None),
        ("POST", f"/projects/{project}/notes", {"body": "intrusion"}),
        ("PATCH", f"/projects/{project}/notes/{n['id']}", {"body": "overwritten"}),
        ("DELETE", f"/projects/{project}/notes/{n['id']}", None),
    ):
        got = client.request(method, url, json=body, headers=H("b"))
        ref = client.request(method, url.replace(project, str(miss)), json=body, headers=H("b"))
        assert (got.status_code, got.json()) == (ref.status_code, ref.json()) == (404, {"detail": "not found"}), (method, url)
    assert su.execute("SELECT body FROM note").fetchall() == [("private thought",)]
    # the owner cannot reach a note through another project of theirs either
    other = client.post("/projects", json={"name": "other"}, headers=H("a")).json()["id"]
    assert client.patch(f"/projects/{other}/notes/{n['id']}", json={"body": "x"}, headers=H("a")).status_code == 404
    assert client.get(f"/projects/{other}/notes", headers=H("a")).json()["notes"] == []
    assert "private thought" not in str(client.get(f"/projects/{project}", headers=H("b")).text)


def test_notes_are_not_in_the_project_payload_or_shared(client, project):
    _mk(client, project, "stay private")
    assert "stay private" not in client.get(f"/projects/{project}", headers=H("a")).text
    assert "stay private" not in client.get("/projects", headers=H("a")).text


@pytest.mark.parametrize("body", [{}, {"body": ""}, {"body": "   \n "}, {"body": None}, {"body": 5}, {"body": "x" * (MAX_NOTE_CHARS + 1)},
                                  {"body": "nul\x00byte"}, {"body": "bell\x07"}, {"body": "ok", "owner_id": str(uuid.uuid4())}, {"body": "ok", "project_id": "x"}])
def test_validation_rejects_bad_bodies_on_create_and_update(client, project, su, body):
    n = _mk(client, project, "keep me")
    assert client.post(f"/projects/{project}/notes", json=body, headers=H("a")).status_code == 422
    assert client.patch(f"/projects/{project}/notes/{n['id']}", json=body, headers=H("a")).status_code == 422
    assert su.execute("SELECT body FROM note").fetchall() == [("keep me",)]


def test_size_cap_boundary_is_accepted(client, project):
    assert len(_mk(client, project, "x" * MAX_NOTE_CHARS)["body"]) == MAX_NOTE_CHARS


def test_note_count_cap(client, project, su):
    owner = su.execute("SELECT owner_id FROM project WHERE id = %s", (project,)).fetchone()[0]
    su.execute("INSERT INTO note (owner_id, project_id, body) SELECT %s, %s, 'n' FROM generate_series(1, %s)", (owner, project, MAX_NOTES_PER_PROJECT))
    r = client.post(f"/projects/{project}/notes", json={"body": "one too many"}, headers=H("a"))
    assert r.status_code == 409 and r.json()["detail"] == "note limit reached"


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_text_is_stored_verbatim_and_never_executed_or_audited(client, project, su, text):
    n = _mk(client, project, text)
    assert n["body"] == text.strip()
    assert client.get(f"/projects/{project}/notes", headers=H("a")).json()["notes"][0]["body"] == text.strip()
    assert su.execute("SELECT count(*) FROM note").fetchone()[0] == 1  # SQL text stayed data
    client.patch(f"/projects/{project}/notes/{n['id']}", json={"body": text + " v2"}, headers=H("a"))
    client.delete(f"/projects/{project}/notes/{n['id']}", headers=H("a"))
    audit = str(su.execute("SELECT * FROM audit_log").fetchall())
    assert text.strip() not in audit and "DROP TABLE" not in audit
    assert {r[0] for r in su.execute("SELECT action FROM audit_log WHERE action LIKE 'note_%'").fetchall()} == {"note_create", "note_update", "note_delete"}


def test_note_routes_do_not_log_the_text(client, project, caplog):
    import logging
    with caplog.at_level(logging.DEBUG):
        n = _mk(client, project, "uniquely-secret-phrase")
        client.patch(f"/projects/{project}/notes/{n['id']}", json={"body": "uniquely-secret-edit"}, headers=H("a"))
        client.post(f"/projects/{project}/notes", json={"body": "x" * (MAX_NOTE_CHARS + 1)}, headers=H("a"))
        client.delete(f"/projects/{project}/notes/{n['id']}", headers=H("a"))
    assert "uniquely-secret" not in caplog.text


def test_deleting_the_project_deletes_its_notes(client, project, su):
    _mk(client, project)
    assert client.delete(f"/projects/{project}", headers=H("a")).status_code == 204
    assert su.execute("SELECT count(*) FROM note").fetchone()[0] == 0


def test_note_routes_need_authentication(client, project):
    for method, url in (("GET", f"/projects/{project}/notes"), ("POST", f"/projects/{project}/notes"), ("PATCH", f"/projects/{project}/notes/{uuid.uuid4()}")):
        assert client.request(method, url, json={"body": "x"}).status_code == 401, url
