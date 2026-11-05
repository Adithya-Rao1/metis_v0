"""Metis Lead Studio HTTP layer. `router` is mounted by `backend.main` under /lead.

`router` is loaded on first access, so the child-process entry points (`runchild`, `heavychild`) do not pull the whole
web stack (database layer, accounts) into the processes that only run RDKit."""

from __future__ import annotations

from typing import Any

__all__ = ["router"]


def __getattr__(name: str) -> Any:
    if name == "router":
        from leadstudio.server.routes import router

        return router
    raise AttributeError(name)
