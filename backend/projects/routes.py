"""Projects, measurements and ranking routes (spec section 11)."""

from __future__ import annotations

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import StringConstraints
from sqlalchemy import text

from backend.jobs.deps import Scope, get_registry, not_found, require_compound, require_project, scope
from backend.ranking.engine import RankingError
from backend.ranking.service import latest_predictions, rank_project
from backend.schemas.contracts import MAX_COMPOUNDS, RankingConfig, Strict
from backend.schemas.interfaces import ModelRegistry

router = APIRouter(tags=["projects"])

ProjectName = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True, pattern=r"^[^\x00-\x1f\x7f]+$")]
Endpoint = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,64}$")]
MAX_PROJECTS_PER_USER = 200


class ProjectCreate(Strict):
    name: ProjectName


class MeasurementCreate(Strict):
    compound_id: uuid.UUID
    endpoint: Endpoint
    value: float
    unit: Optional[Annotated[str, StringConstraints(max_length=32, pattern=r"^[^\x00-\x1f\x7f]*$")]] = None
    assay: Optional[Annotated[str, StringConstraints(max_length=200, pattern=r"^[^\x00-\x1f\x7f]*$")]] = None


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate, sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        n = s.execute(text("SELECT count(*) FROM project WHERE owner_id = :uid"), {"uid": sc.user.id}).scalar_one()
        if n >= MAX_PROJECTS_PER_USER:
            raise HTTPException(status_code=409, detail="project limit reached")
        row = s.execute(
            text("INSERT INTO project (owner_id, name) VALUES (:uid, :name) RETURNING id, name, created_at"),
            {"uid": sc.user.id, "name": body.name},
        ).mappings().one()
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"], "compound_count": 0, "max_compounds": MAX_COMPOUNDS}


@router.get("/projects")
def list_projects(sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        rows = s.execute(
            text(
                "SELECT p.id, p.name, p.created_at, (SELECT count(*) FROM compound c WHERE c.project_id = p.id AND c.owner_id = p.owner_id) AS n "
                "FROM project p WHERE p.owner_id = :uid ORDER BY p.created_at DESC, p.id LIMIT 200"
            ),
            {"uid": sc.user.id},
        ).mappings().all()
    return {"projects": [{"id": r["id"], "name": r["name"], "created_at": r["created_at"], "compound_count": r["n"]} for r in rows]}


@router.get("/projects/{project_id}")
def get_project(project_id: uuid.UUID, sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        proj = require_project(s, sc.user.id, project_id)
        comps = s.execute(
            text(
                "SELECT id, smiles, canonical_smiles, label, created_at FROM compound "
                "WHERE project_id = :pid AND owner_id = :uid ORDER BY created_at, id"
            ),
            {"pid": project_id, "uid": sc.user.id},
        ).mappings().all()
        latest = latest_predictions(s, sc.user.id, project_id)
        meas = s.execute(
            text(
                "SELECT m.id, m.compound_id, m.endpoint, m.value, m.unit, m.assay, m.created_at FROM measurement m "
                "JOIN compound c ON c.id = m.compound_id AND c.owner_id = m.owner_id "
                "WHERE c.project_id = :pid AND m.owner_id = :uid ORDER BY m.created_at, m.id"
            ),
            {"pid": project_id, "uid": sc.user.id},
        ).mappings().all()
    by_c: dict[uuid.UUID, list[dict]] = {}
    for m in meas:
        by_c.setdefault(m["compound_id"], []).append({**dict(m), "label": "measured"})
    return {
        "id": proj["id"],
        "name": proj["name"],
        "created_at": proj["created_at"],
        "max_compounds": MAX_COMPOUNDS,
        "compounds": [
            {
                **dict(c),
                "predictions": [
                    {"id": pid, "label": "predicted", "prediction": pred.model_dump(mode="json")}
                    for pid, pred in latest.get(c["id"], {}).values()
                ],
                "measurements": by_c.get(c["id"], []),
            }
            for c in comps
        ],
    }


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: uuid.UUID, sc: Scope = Depends(scope)) -> Response:
    with sc.session() as s:
        res = s.execute(text("DELETE FROM project WHERE id = :pid AND owner_id = :uid"), {"pid": project_id, "uid": sc.user.id})
        if res.rowcount == 0:
            raise not_found()
    return Response(status_code=204)


@router.post("/projects/{project_id}/measurements", status_code=201)
def add_measurement(project_id: uuid.UUID, body: MeasurementCreate, sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        require_project(s, sc.user.id, project_id)
        require_compound(s, sc.user.id, project_id, body.compound_id)
        row = s.execute(
            text(
                "INSERT INTO measurement (owner_id, compound_id, endpoint, value, unit, assay) "
                "VALUES (:uid, :cid, :ep, :v, :unit, :assay) RETURNING id, created_at"
            ),
            {"uid": sc.user.id, "cid": body.compound_id, "ep": body.endpoint, "v": body.value, "unit": body.unit, "assay": body.assay},
        ).mappings().one()
    return {"id": row["id"], "compound_id": body.compound_id, "endpoint": body.endpoint, "value": body.value, "unit": body.unit, "assay": body.assay, "label": "measured", "created_at": row["created_at"]}


@router.put("/projects/{project_id}/ranking")
def put_ranking(project_id: uuid.UUID, config: RankingConfig, sc: Scope = Depends(scope), registry: ModelRegistry = Depends(get_registry)) -> dict:
    """Validate the ranking config and return the ranking computed from the latest stored predictions.

    The config is NOT persisted: the project table has no column for it (needs a migration; see handoff).
    """
    with sc.session() as s:
        require_project(s, sc.user.id, project_id)
        try:
            ranked = rank_project(s, registry, sc.user.id, project_id, config)
        except RankingError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
    return {
        "config": config.model_dump(mode="json"),
        "persisted": False,
        "formula": "R_j = (1 - lam) S_j + lam C_j",
        "ranking": [r.model_dump(mode="json") for r in ranked],
    }

