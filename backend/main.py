"""App factory. Mounts each package's router if it exists (see backend/schemas/interfaces.py)."""
from __future__ import annotations

import importlib

from fastapi import FastAPI

from backend.accounts.hardening import install as install_hardening

ROUTERS = ("backend.accounts", "backend.projects", "backend.dockprep")


def create_app() -> FastAPI:
    app = FastAPI(title="Metis", docs_url=None, redoc_url=None, openapi_url=None)

    install_hardening(app)

    @app.get("/health")
    def health() -> dict:
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
