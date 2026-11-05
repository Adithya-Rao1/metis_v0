"""Glue between stored predictions, the model registry and the pure ranking engine."""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.jobs.deps import row_to_prediction
from backend.ranking.engine import EndpointInfo, RankedCompound, RankingError, rank_compounds
from backend.schemas.contracts import Prediction, RankingConfig
from backend.schemas.interfaces import ModelCard, ModelRegistry


def _width_table(card: ModelCard) -> tuple[dict[int, float], Optional[float]]:
    """Per-N median calibration widths: the ModelCard field first, then (legacy) benchmark keys."""
    by_n: dict[int, float] = {}
    legacy = card.benchmark.get("median_calibration_width_by_n")
    for src in (legacy if isinstance(legacy, dict) else {}, card.median_calibration_width_by_n):
        for k, v in src.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and str(k).isdigit():
                by_n[int(k)] = float(v)
    v = card.benchmark.get("median_calibration_width")
    return by_n, (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None)


def median_width(card: ModelCard, ensemble_n: int) -> float | None:
    """Median calibration interval width for this ensemble size (ModelCard.median_calibration_width_by_n[str(N)])."""
    by_n, generic = _width_table(card)
    return by_n.get(ensemble_n, generic)


def endpoint_info(registry: ModelRegistry, endpoint: str) -> EndpointInfo:
    try:
        card = registry.model_card(endpoint)
    except LookupError:
        raise RankingError(f"unknown endpoint {endpoint}") from None
    by_n, generic = _width_table(card)
    return EndpointInfo(default_range=card.default_range, median_width=generic, median_width_by_n=by_n)


def latest_predictions(
    session: Session, owner_id: uuid.UUID, project_id: uuid.UUID
) -> dict[uuid.UUID, dict[str, tuple[uuid.UUID, Prediction]]]:
    rows = (
        session.execute(
            text(
                "SELECT DISTINCT ON (p.compound_id, p.endpoint) p.id, p.compound_id, p.endpoint, p.model_version_id, p.ensemble_n, "
                "p.kind, p.value, p.interval_low, p.interval_high, p.probability, p.coverage, p.sample_size, p.method_note, "
                "p.uncertainty_breakdown FROM prediction p JOIN compound c ON c.id = p.compound_id AND c.owner_id = p.owner_id "
                "WHERE c.project_id = :pid AND p.owner_id = :uid ORDER BY p.compound_id, p.endpoint, p.created_at DESC, p.id"
            ),
            {"pid": project_id, "uid": owner_id},
        )
        .mappings()
        .all()
    )
    out: dict[uuid.UUID, dict[str, tuple[uuid.UUID, Prediction]]] = {}
    for r in rows:
        out.setdefault(r["compound_id"], {})[r["endpoint"]] = (r["id"], row_to_prediction(r))
    return out


def current_version_id(registry: ModelRegistry, endpoint: str, smiles: str, n: int) -> uuid.UUID | None:
    """The live model version for an endpoint: the registry's own answer if it offers one, else a probe prediction
    (the same technique the /uncertainty route uses)."""
    fn = getattr(registry, "current_model_version_id", None)
    if callable(fn):
        try:
            return fn(endpoint)
        except (LookupError, ValueError):
            return None
    try:
        return registry.predict(endpoint, smiles, n).prediction.model_version_id
    except (LookupError, ValueError):
        return None


def rank_project(
    session: Session,
    registry: ModelRegistry,
    owner_id: uuid.UUID,
    project_id: uuid.UUID,
    config: RankingConfig,
) -> tuple[list[RankedCompound], list[dict]]:
    """Returns (ranking, stale). `stale` lists stored predictions whose model version is not the live one: they are
    still ranked (using each prediction's own interval and N) but flagged, never silently mixed."""
    ids = [
        r[0]
        for r in session.execute(
            text(
                "SELECT id FROM compound WHERE project_id = :pid AND owner_id = :uid ORDER BY created_at, id"
            ),
            {"pid": project_id, "uid": owner_id},
        )
    ]
    smiles = {
        r[0]: r[1]
        for r in session.execute(
            text("SELECT id, canonical_smiles FROM compound WHERE project_id = :pid AND owner_id = :uid"),
            {"pid": project_id, "uid": owner_id},
        )
    }
    latest = latest_predictions(session, owner_id, project_id)
    preds: dict[str, dict[str, Prediction]] = {
        str(cid): {e: p for e, (_, p) in latest.get(cid, {}).items()} for cid in ids
    }
    endpoints = {w.endpoint for w in config.weights} | {lim.endpoint for lim in config.limits}
    info = {e: endpoint_info(registry, e) for e in endpoints}
    ranked = rank_compounds(preds, config, info)
    live: dict[str, uuid.UUID | None] = {}
    stale: list[dict] = []
    for cid in ids:
        for e in sorted(endpoints):
            p = preds[str(cid)].get(e)
            if p is None:
                continue
            if e not in live:
                live[e] = current_version_id(registry, e, smiles[cid], p.ensemble_n)
            if live[e] is None or live[e] != p.model_version_id:
                stale.append(
                    {
                        "compound_id": str(cid),
                        "endpoint": e,
                        "model_version_id": str(p.model_version_id),
                        "reason": "stale: predicted with a different model version than the current one; re-run predictions"
                        if live[e] is not None
                        else "stale: current model version could not be verified",
                    }
                )
    return ranked, stale
