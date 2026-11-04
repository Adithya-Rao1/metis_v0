"""One-origin test site: serves web/ statically and the API under /api on the SAME origin.

Assumption (production routing is not decided here): a reverse proxy sends `/api/*` to the FastAPI app with the
`/api` prefix stripped, and serves everything else from web/. This module does exactly that in one ASGI app, and
applies web/_headers (Netlify/Cloudflare semantics: every matching rule applies, same-named headers are combined)
so the browser enforces the real Content-Security-Policy.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from pathlib import Path

import uvicorn
from starlette.staticfiles import StaticFiles

API_PREFIX = "/api"


def parse_headers_file(path: Path) -> list[tuple[re.Pattern, dict[str, str]]]:
    rules: list[tuple[re.Pattern, dict[str, str]]] = []
    pattern = None
    for raw in path.read_text().splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith((" ", "\t")):
            rx = re.escape(raw.strip()).replace(r"\*", ".*")
            rx = re.sub(r"\\:[A-Za-z_]+", "[^/]+", rx)
            pattern = re.compile(f"^{rx}$")
            rules.append((pattern, {}))
        elif pattern is not None:
            k, _, v = raw.strip().partition(":")
            rules[-1][1][k.strip()] = v.strip()
    return rules


def headers_for(path: str, rules) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    for rx, hdrs in rules:
        if rx.match(path):
            for k, v in hdrs.items():
                out.setdefault(k, []).append(v)
    return {k: ", ".join(v) for k, v in out.items()}


class Site:
    def __init__(self, api_app, web_dir: Path):
        self.api = api_app
        self.rules = parse_headers_file(web_dir / "_headers")
        self.static = StaticFiles(directory=str(web_dir), html=True)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.api(scope, receive, send)
            return
        path = scope["path"]
        if path == API_PREFIX or path.startswith(API_PREFIX + "/"):
            scope = dict(scope)
            scope["path"] = path[len(API_PREFIX) :] or "/"
            if "raw_path" in scope and scope["raw_path"]:
                scope["raw_path"] = scope["raw_path"][len(API_PREFIX) :] or b"/"
            await self.api(scope, receive, send)
            return
        extra = headers_for(path, self.rules)

        async def send2(message):
            if message["type"] == "http.response.start":
                hdrs = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.decode().lower() not in {x.lower() for x in extra}
                ]
                hdrs += [(k.lower().encode(), v.encode()) for k, v in extra.items()]
                message = {**message, "headers": hdrs}
            await send(message)

        await self.static(scope, receive, send2)


class LiveServer:
    """uvicorn in a thread on a free local port."""

    def __init__(self, app):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("test server did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
