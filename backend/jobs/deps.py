"""Shared FastAPI dependencies for the domain API (kept outside backend.projects to avoid import cycles).

Identity reaches Postgres ONLY through backend.core.db.request_session. Every owned-resource lookup also
filters on owner_id explicitly (RLS also lets grantees SELECT shared projects, which is not what these
owner-only routes want) and answers 404, never 403, for other users' resources.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from backend.core.db import make_engine, make_session_factory, request_session
from backend.jobs.queue import InProcessJobQueue, JobQueue
from backend.schemas.contracts import Prediction
from backend.schemas.interfaces import ModelRegistry, UserCtx

try:  # Agent 4 provides this; until it lands every domain route fails closed.
    from backend.accounts.deps import current_user
except ImportError:  # pragma: no cover - exercised only before accounts is merged

    def current_user() -> UserCtx:  # type: ignore[misc]
        raise HTTPException(status_code=401, detail="authentication unavailable")


MAX_BODY_BYTES = 2_000_000


@lru_cache
def get_factory() -> sessionmaker[Session]:
    return make_session_factory(make_engine())


@lru_cache
def get_queue() -> JobQueue:
    return InProcessJobQueue()


def get_registry() -> ModelRegistry:
    """Concrete registry comes from ml/registry (Agent 5). Absent -> 503; tests override this dependency."""
    try:
        from ml.registry import get_registry as _get  # type: ignore[import-not-found]
    except ImportError:
        raise HTTPException(status_code=503, detail="model registry unavailable") from None
    return _get()


@dataclass
class Scope:
    user: UserCtx
    factory: sessionmaker[Session]

    @contextmanager
    def session(self) -> Iterator[Session]:
        with request_session(self.factory, self.user.id) as s:
            yield s


def scope(user: UserCtx = Depends(current_user), factory: sessionmaker[Session] = Depends(get_factory)) -> Scope:
    return Scope(user, factory)


def limit_body(request: Request) -> None:
    cl = request.headers.get("content-length")
    if cl is not None and (not cl.isdigit() or int(cl) > MAX_BODY_BYTES):
        raise HTTPException(status_code=413, detail="request body too large")


def not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="not found")


def require_project(session: Session, user_id: uuid.UUID, project_id: uuid.UUID) -> dict[str, Any]:
    row = (
        session.execute(
            text("SELECT id, name, created_at FROM project WHERE id = :pid AND owner_id = :uid"),
            {"pid": project_id, "uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found()
    return dict(row)


def require_compound(session: Session, user_id: uuid.UUID, project_id: uuid.UUID, compound_id: uuid.UUID) -> dict[str, Any]:
    row = (
        session.execute(
            text(
                "SELECT id, project_id, smiles, canonical_smiles, label, created_at FROM compound "
                "WHERE id = :cid AND project_id = :pid AND owner_id = :uid"
            ),
            {"cid": compound_id, "pid": project_id, "uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found()
    return dict(row)


def row_to_prediction(row: Any) -> Prediction:
    return Prediction(
        endpoint=row["endpoint"],
        kind=row["kind"],
        model_version_id=row["model_version_id"],
        ensemble_n=row["ensemble_n"],
        value=row["value"],
        interval_low=row["interval_low"],
        interval_high=row["interval_high"],
        probability=row["probability"],
        coverage=row["coverage"],
        sample_size=row["sample_size"],
        method_note=row["method_note"],
        breakdown=row["uncertainty_breakdown"],
    )

