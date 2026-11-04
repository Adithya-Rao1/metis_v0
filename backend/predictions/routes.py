"""Prediction jobs, job status, uncertainty detail and (public) model cards."""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import text

from backend.jobs.deps import (
    Scope,
    get_queue,
    get_registry,
    not_found,
    require_project,
    row_to_prediction,
    scope,
)
from backend.jobs.queue import JobQueue, Progress
from backend.schemas.contracts import DEFAULT_N, MAX_ENDPOINTS, Endpoint, Strict, allowed_ensemble_sizes
from backend.schemas.interfaces import ModelRegistry, PredictionDetail

log = logging.getLogger("metis.predictions")
router = APIRouter(tags=["predictions"])
public_router = APIRouter(tags=["models"])

_INSERT = text(
    "INSERT INTO prediction (owner_id, compound_id, endpoint, model_version_id, ensemble_n, kind, value, interval_low, "
    "interval_high, probability, coverage, sample_size, method_note, uncertainty_breakdown) VALUES "
    "(:uid, :cid, :endpoint, :mv, :n, :kind, :value, :lo, :hi, :prob, :cov, :ss, :note, CAST(:bd AS jsonb)) RETURNING id"
)


class PredictRequest(Strict):
    endpoints: list[Endpoint] = Field(min_length=1, max_length=MAX_ENDPOINTS)
    ensemble_n: Literal[3, 5, 10, 20] = DEFAULT_N


def _estimate(registry: ModelRegistry, endpoints: list[str], n: int, n_compounds: int) -> float:
    total = 0.0
    for e in endpoints:
        try:
            registry.model_card(e)
            total += float(registry.time_estimate_seconds(e, n, n_compounds))
        except (LookupError, ValueError):
            raise HTTPException(status_code=422, detail=f"unknown endpoint: {e}") from None
    return total


@router.post("/projects/{project_id}/predict", status_code=202)
def predict(
    project_id: uuid.UUID,
    body: PredictRequest,
    sc: Scope = Depends(scope),
    registry: ModelRegistry = Depends(get_registry),
    queue: JobQueue = Depends(get_queue),
) -> dict:
    if body.ensemble_n not in allowed_ensemble_sizes(sc.user.tier):
        raise HTTPException(status_code=422, detail=f"ensemble size {body.ensemble_n} is not available on the {sc.user.tier} tier")
    if len(set(body.endpoints)) != len(body.endpoints):
        raise HTTPException(status_code=422, detail="duplicate endpoints")
    with sc.session() as s:
        require_project(s, sc.user.id, project_id)
        comps = [
            (r[0], r[1])
            for r in s.execute(
                text("SELECT id, canonical_smiles FROM compound WHERE project_id = :pid AND owner_id = :uid ORDER BY created_at, id"),
                {"pid": project_id, "uid": sc.user.id},
            )
        ]
    if not comps:
        raise HTTPException(status_code=422, detail="project has no compounds")
    estimate = _estimate(registry, body.endpoints, body.ensemble_n, len(comps))
    owner, factory, n, endpoints = sc.user.id, sc.factory, body.ensemble_n, list(body.endpoints)

    def work(progress: Progress) -> None:
        from backend.core.db import request_session

        for cid, smiles in comps:
            for endpoint in endpoints:
                detail = registry.predict(endpoint, smiles, n)
                p = detail.prediction
                if p.endpoint != endpoint or p.ensemble_n != n:
                    raise ValueError("registry returned a mismatched prediction")
                with request_session(factory, owner) as s:
                    pid = s.execute(
                        _INSERT,
                        {
                            "uid": owner, "cid": cid, "endpoint": endpoint, "mv": p.model_version_id, "n": p.ensemble_n,
                            "kind": p.kind, "value": p.value, "lo": p.interval_low, "hi": p.interval_high,
                            "prob": p.probability, "cov": p.coverage, "ss": p.sample_size, "note": p.method_note,
                            "bd": p.breakdown.model_dump_json(),
                        },
                    ).scalar_one()
                progress(pid)

    rec = queue.submit(owner, project_id, len(comps) * len(endpoints), estimate, work)
    return {"job_id": rec.id, "state": rec.state, "total": rec.total, "estimated_seconds": estimate, "ensemble_n": n, "label": "predicted"}


@router.get("/jobs/{job_id}")
def get_job(job_id: uuid.UUID, sc: Scope = Depends(scope), queue: JobQueue = Depends(get_queue)) -> dict:
    rec = queue.get(sc.user.id, job_id)
    if rec is None:
        raise not_found()
    return {
        "job_id": rec.id, "project_id": rec.project_id, "state": rec.state, "done": rec.done, "total": rec.total,
        "estimated_seconds": rec.estimate_seconds, "error": rec.error, "prediction_ids": rec.prediction_ids,
    }


@router.get("/predictions/{prediction_id}/uncertainty", response_model=PredictionDetail)
def uncertainty(prediction_id: uuid.UUID, sc: Scope = Depends(scope), registry: ModelRegistry = Depends(get_registry)) -> PredictionDetail:
    """Full PredictionDetail. Only the Prediction core is persisted; fragments, neighbours and runtime come from
    re-running the same model version (409 if that version is no longer the live one)."""
    with sc.session() as s:
        row = s.execute(
            text(
                "SELECT p.endpoint, p.model_version_id, p.ensemble_n, p.kind, p.value, p.interval_low, p.interval_high, p.probability, "
                "p.coverage, p.sample_size, p.method_note, p.uncertainty_breakdown, c.canonical_smiles "
                "FROM prediction p JOIN compound c ON c.id = p.compound_id AND c.owner_id = p.owner_id "
                "WHERE p.id = :id AND p.owner_id = :uid"
            ),
            {"id": prediction_id, "uid": sc.user.id},
        ).mappings().first()
    if row is None:
        raise not_found()
    stored = row_to_prediction(row)
    try:
        fresh = registry.predict(row["endpoint"], row["canonical_smiles"], row["ensemble_n"])
    except (LookupError, ValueError):
        raise HTTPException(status_code=409, detail="model is no longer available for detail") from None
    if fresh.prediction.model_version_id != stored.model_version_id:
        raise HTTPException(status_code=409, detail="model version has changed since this prediction was made")
    return PredictionDetail(prediction=stored, fragments=fresh.fragments, nearest_training=fresh.nearest_training, runtime_seconds=fresh.runtime_seconds)


@public_router.get("/models")
def list_models(registry: ModelRegistry = Depends(get_registry)) -> dict:
    return {"models": [c.model_dump(mode="json") for c in registry.list_models()]}


@public_router.get("/models/{name}")
def get_model(name: str, registry: ModelRegistry = Depends(get_registry)) -> dict:
    if not (1 <= len(name) <= 64) or not all(ch.islower() or ch.isdigit() or ch == "_" for ch in name) or not name.isascii():
        raise not_found()
    try:
        return registry.model_card(name).model_dump(mode="json")
    except LookupError:
        raise not_found() from None

