"""Wave 2 accounts fixes: lock expiry, CSRF binding, body guard, NaN rejection, bucket purge."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.accounts import deps, hardening
from backend.accounts.config import COOKIE_CSRF, CSRF_HEADER


def test_lock_expiry_resets_counter(client, su):
    client.signup_login("v@example.com")
    c2 = type(client)(client.app)
    for _ in range(5):
        c2.login("v@example.com", "wrongwrongwrong")
    assert su.execute("SELECT locked_until > now() FROM app_user").fetchone()[0]
    su.execute("UPDATE app_user SET locked_until = now() - interval '1 minute'")
    c2.login("v@example.com", "wrongwrongwrong")
    assert not su.execute("SELECT locked_until > now() FROM app_user").fetchone()[0]
    assert c2.login("v@example.com").status_code == 200


def test_csrf_bound_to_session(client, client2):
    client.signup_login("a@example.com")
    anon = client2.csrf()
    client.http.cookies.set(COOKIE_CSRF, anon)
    r = client.http.post("/auth/logout", headers={CSRF_HEADER: anon})
    assert r.status_code == 403


def test_csrf_expired_rejected(client, monkeypatch):
    tok = client.csrf()
    real = __import__("time").time
    monkeypatch.setattr("backend.accounts.security.time.time", lambda: real() + 7200)
    assert client.http.post("/auth/login", json={"email": "x@example.com", "password": "y" * 12},
                            headers={CSRF_HEADER: tok}).status_code == 403


def test_expired_buckets_purged(client, su, factory):
    su.execute(
        "INSERT INTO rate_limit_bucket (key_hash, window_start) VALUES (repeat('a', 64), now() - interval '1 day')"
    )
    assert deps.purge_expired_buckets(factory) == 1
    assert su.execute("SELECT count(*) FROM rate_limit_bucket").fetchone()[0] == 0


@pytest.fixture
def guarded():
    app = FastAPI()
    hardening.install(app)

    @app.post("/x")
    async def x(payload: dict) -> dict:
        return {"ok": True}

    @app.post("/dockprep/y")
    async def y(payload: dict) -> dict:
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_nan_and_infinity_are_422(guarded):
    for body in ('{"a": NaN}', '{"a": [Infinity]}', '{"a": -Infinity}'):
        r = guarded.post("/x", content=body, headers={"content-type": "application/json"})
        assert r.status_code == 422, body
    assert guarded.post("/x", json={"a": 1}).status_code == 200


def test_body_cap_chunked_and_declared(guarded):
    def gen(n):
        for _ in range(n):
            yield b" " * 1000

    assert guarded.post("/x", content=gen(3000), headers={"content-type": "application/json"}).status_code == 413
    big = b'{"a":"' + b"x" * (3 * 1024 * 1024) + b'"}'
    assert guarded.post("/x", content=big, headers={"content-type": "application/json"}).status_code == 413
    assert guarded.post("/dockprep/y", content=big, headers={"content-type": "application/json"}).status_code == 200
