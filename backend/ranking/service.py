"""Glue between stored predictions, the model registry and the pure ranking engine."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.jobs.deps import row_to_prediction
from backend.ranking.engine import EndpointInfo, RankedCompound, RankingError, rank_compounds
from backend.schemas.contracts import Prediction, RankingConfig
from backend.schemas.interfaces import ModelCard, ModelRegistry


def median_width(card: ModelCard, ensemble_n: int) -> float | None:
    """Median calibration interval width from the model card benchmark block.

    Expected keys (a request to Agent 5/integrator, ModelCard has no dedicated field):
    benchmark["median_calibration_width_by_n"][str(N)] or benchmark["median_calibration_width"].
    """
    by_n = card.benchmark.get("median_calibration_width_by_n")
    if isinstance(by_n, dict) and str(ensemble_n) in by_n:
        return float(by_n[str(ensemble_n)])
    v = card.benchmark.get("median_calibration_width")
    return float(v) if isinstance(v, (int, float)) else None


def endpoint_info(registry: ModelRegistry, endpoint: str, ensemble_n: int) -> EndpointInfo:
    try:
        card = registry.model_card(endpoint)
    except LookupError:
        raise RankingError(f"unknown endpoint {endpoint}") from None
    return EndpointInfo(default_range=card.default_range, median_width=median_width(card, ensemble_n))


def latest_predictions(session: Session, owner_id: uuid.UUID, project_id: uuid.UUID) -> dict[uuid.UUID, dict[str, tuple[uuid.UUID, Prediction]]]:
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


def rank_project(session: Session, registry: ModelRegistry, owner_id: uuid.UUID, project_id: uuid.UUID, config: RankingConfig) -> list[RankedCompound]:
    ids = [r[0] for r in session.execute(text("SELECT id FROM compound WHERE project_id = :pid AND owner_id = :uid ORDER BY created_at, id"), {"pid": project_id, "uid": owner_id})]
    latest = latest_predictions(session, owner_id, project_id)
    preds: dict[str, dict[str, Prediction]] = {str(cid): {e: p for e, (_, p) in latest.get(cid, {}).items()} for cid in ids}
    endpoints = {w.endpoint for w in config.weights} | {lim.endpoint for lim in config.limits}
    info: dict[str, EndpointInfo] = {}
    for e in endpoints:
        n_used = next((p[e].ensemble_n for p in preds.values() if e in p), 5)
        info[e] = endpoint_info(registry, e, n_used)
    return rank_compounds(preds, config, info)

