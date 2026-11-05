"""App factory. Mounts each package's router if it exists (see backend/schemas/interfaces.py)."""

from __future__ import annotations

import importlib

from fastapi import FastAPI, Request, Response
from sqlalchemy import text

from backend.accounts.deps import get_factory
from backend.accounts.hardening import install as install_hardening

# "leadstudio.server" = Lead Studio (Beta): router with the /lead prefix, Metis session required.
ROUTERS = ("backend.accounts", "backend.projects", "backend.dockprep", "leadstudio.server")


def create_app() -> FastAPI:
    app = FastAPI(title="Metis", docs_url=None, redoc_url=None, openapi_url=None)

    install_hardening(app)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(request: Request, response: Response) -> dict:
        """Readiness: can the app reach its database? Reads nothing else; no data and no error detail."""
        try:
            with get_factory(request)() as s:
                s.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - any failure means "not ready"; the reason is deliberately not exposed
            response.status_code = 503
            response.headers["Cache-Control"] = "no-store"
            return {"status": "unavailable"}
        return {"status": "ok"}

    for mod in ROUTERS:
        try:
            m = importlib.import_module(mod)
        except ImportError:
            continue
        router = getattr(m, "router", None)
        if router is not None:
            app.include_router(router)
    return app


app = create_app()
