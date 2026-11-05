"""Auth dependency. Uses Agent 4's `backend.accounts.deps.current_user`; if it is not importable the endpoints fail
closed (401), never open."""

from __future__ import annotations

from fastapi import HTTPException

try:  # pragma: no cover - depends on parallel work
    from backend.accounts.deps import current_user  # type: ignore[import-not-found]
except ImportError:

    def current_user():  # type: ignore[misc]
        raise HTTPException(status_code=401, detail="Authentication unavailable")
