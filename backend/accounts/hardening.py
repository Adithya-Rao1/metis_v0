"""App-wide request hardening (pure ASGI): body-size cap that also counts chunked bodies (F-70), and
rejection of non-finite JSON constants (NaN/Infinity/-Infinity) before they can crash error rendering (F-66)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

DEFAULT_MAX_BODY = 2 * 1024 * 1024
DOCKPREP_MAX_BODY = 22 * 1024 * 1024
DOCKPREP_PREFIX = "/dockprep"


def max_body_for(path: str) -> int:
    if path == DOCKPREP_PREFIX or path.startswith(DOCKPREP_PREFIX + "/"):
        return DOCKPREP_MAX_BODY
    return DEFAULT_MAX_BODY


class _NonFinite(ValueError):
    pass


def _reject_constant(name: str) -> Any:
    raise _NonFinite(name)


def has_non_finite_json(body: bytes) -> bool:
    try:
        json.loads(body, parse_constant=_reject_constant)
    except _NonFinite:
        return True
    except ValueError:  # malformed JSON / bad encoding: the framework reports it as usual
        return False
    return False


def _is_json(headers: list[tuple[bytes, bytes]]) -> bool:
    for k, v in headers:
        if k == b"content-type":
            return b"json" in v.lower()
    return False


class BodyGuardMiddleware:
    """Buffers each request body (bounded by the per-route cap), answers 413 / 422 itself, then replays it."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cap = max_body_for(scope.get("path", ""))
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        for k, v in headers:
            if k == b"content-length":
                if not v.isdigit() or int(v) > cap:
                    await self._reply(send, 413, "request body too large")
                    return
        chunks: list[bytes] = []
        size = 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            part = msg.get("body", b"")
            size += len(part)
            if size > cap:
                await self._reply(send, 413, "request body too large")
                return
            chunks.append(part)
            if not msg.get("more_body", False):
                break
        body = b"".join(chunks)
        if body and _is_json(headers) and has_non_finite_json(body):
            await self._reply(
                send, 422, [{"type": "json_invalid", "loc": ["body"], "msg": "NaN/Infinity not allowed"}]
            )
            return
        sent = False

        async def replay() -> dict:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _reply(send: Any, status: int, detail: Any) -> None:
        resp = JSONResponse({"detail": detail}, status_code=status, headers={"Connection": "close"})
        await resp({"type": "http"}, _never_receive, send)


async def _never_receive() -> dict:
    return {"type": "http.disconnect"}


async def _validation_handler(_request: Any, exc: RequestValidationError) -> JSONResponse:
    """Like FastAPI's default but drops echoed `input` values that cannot be rendered as strict JSON."""
    errors = []
    for e in exc.errors():
        e = dict(e)
        try:
            json.dumps(jsonable_encoder(e), allow_nan=False)
        except (ValueError, TypeError):
            e.pop("input", None)
            e.pop("ctx", None)
        errors.append(e)
    return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)


def install(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_middleware(BodyGuardMiddleware)
