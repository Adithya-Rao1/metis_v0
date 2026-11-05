"""Fixtures for the Wave 4 red-team suite (Lead Studio + launch readiness): the REAL Metis session stack on pgserver (no dependency
overrides for authentication), a mock-mode Lead Studio state, and a few helpers. Test-only. `pg` comes from tests/security/conftest.py."""
from __future__ import annotations

import pathlib
import sys

import psycopg
import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (HERE, ROOT, ROOT / "tests" / "security" / "wave2", ROOT / "tests" / "security" / "wave3", ROOT / "tests" / "api"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import w2stack as S  # noqa: E402
from api_support import FakeRegistry  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from w3util import HI  # noqa: E402

from backend.accounts.config import AccountsConfig  # noqa: E402
from leadstudio.server.core import Settings  # noqa: E402
from leadstudio.server.state import build_state  # noqa: E402

NITRO = "CCOC(=O)c1ccc(cc1)[N+](=O)[O-]"
ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"


@pytest.fixture(scope="module")
def w4(pg):
    factory, engine = S.login_factory(pg, role="w4_app_login")
    yield factory
    engine.dispose()


@pytest.fixture
def su4(pg):
    with psycopg.connect(pg, autocommit=True) as c:
        yield c
        c.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_no_update")
        c.execute("TRUNCATE app_user, model_version, deletion_request, audit_log, rate_limit_bucket, session_revocation CASCADE")
        c.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_no_update")


@pytest.fixture
def mk4(w4, su4):
    """Factory: the full app (accounts, domain, dockprep, Lead Studio) with generous account rate limits."""

    def make(queue=None, **cfg):
        su4.execute("INSERT INTO model_version (id, endpoint, version) VALUES (%s,'test','t0') ON CONFLICT DO NOTHING", (str(FakeRegistry.version_id),))
        return S.build_app(w4, FakeRegistry(), queue, AccountsConfig(**{**HI, **cfg}))

    return make


def mock_state(**kw):
    base = dict(force_mock=True, mock_delay=0.0, replay_delay=0.0, runs_per_minute=10**6, validates_per_minute=10**6)
    base.update(kw)
    return build_state(settings=Settings(**base))


@pytest.fixture
def lead(mk4):
    app = mk4()
    app.state.lead = mock_state(per_user_active=10)
    return app


def tolerant(c: S.Cl) -> S.Cl:
    """Same session, but a server-side exception becomes a 500 response instead of being re-raised into the test."""
    old = c.http
    c.http = TestClient(c.app, base_url=str(old.base_url), raise_server_exceptions=False)
    c.http.cookies.update(old.cookies)
    return c


def signed_in(app, email="a@example.org", **kw) -> S.Cl:
    c = S.Cl(app)
    c.signup(email)
    return tolerant(c) if kw.get("tolerant", True) else c


def raw_asgi(app, method: str, path: str, headers: list[tuple[bytes, bytes]], body: bytes = b"", disconnect_after_start: bool = False):
    """Drive the ASGI app with RAW header bytes (what uvicorn/h11 deliver; httpx re-encodes non-ASCII header values, so it
    cannot send them). Returns (status, response_body, exception-or-None)."""
    import asyncio

    out: dict = {"status": None, "body": b"", "exc": None}

    async def go():
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            if disconnect_after_start:
                return {"type": "http.disconnect"}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "https", "path": path,
                 "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": [(b"host", b"testserver"), *headers],
                 "client": ("127.0.0.1", 50000), "server": ("testserver", 443)}
        try:
            await asyncio.wait_for(app(scope, receive, send), timeout=10)
        except asyncio.TimeoutError:
            out["exc"] = "timeout"
        except Exception as exc:  # noqa: BLE001 - the point is to observe it
            out["exc"] = exc

    t = __import__('threading').Thread(target=lambda: asyncio.run(go()))     # own thread: a loop may already be running here
    t.start()
    t.join()
    return out["status"], out["body"], out["exc"]


def cookie_header(c: S.Cl) -> tuple[bytes, bytes]:
    return b"cookie", "; ".join(f"{k}={v}" for k, v in c.http.cookies.items()).encode()
