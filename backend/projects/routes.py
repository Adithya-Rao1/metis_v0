"""Projects, measurements and ranking routes (spec section 11)."""

from __future__ import annotations

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import StringConstraints
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.jobs.deps import Scope, get_registry, not_found, require_compound, require_project, scope
from backend.ranking.engine import RankingError
from backend.ranking.service import latest_predictions, rank_project
from backend.schemas.contracts import MAX_COMPOUNDS, RankingConfig, Strict
from backend.schemas.interfaces import ModelRegistry

router = APIRouter(tags=["projects"])

ProjectName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=200, strip_whitespace=True, pattern=r"^[^\x00-\x1f\x7f]+$"),
]
Endpoint = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,64}$")]
MAX_PROJECTS_PER_USER = 200
# Notes belong to a project (the `note` table has no compound column). Free text: newlines and tabs are fine, other
# control characters are not. The table allows 20000 characters; the API is stricter. Bodies are never logged or audited.
MAX_NOTE_CHARS = 5000
MAX_NOTES_PER_PROJECT = 200
NoteBody = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=MAX_NOTE_CHARS,
        strip_whitespace=True,
        pattern=r"^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+$",
    ),
]


class ProjectCreate(Strict):
    name: ProjectName


class ProjectRename(Strict):
    name: ProjectName


class NoteIn(Strict):
    body: NoteBody


class MeasurementCreate(Strict):
    compound_id: uuid.UUID
    endpoint: Endpoint
    value: float
    unit: Optional[Annotated[str, StringConstraints(max_length=32, pattern=r"^[^\x00-\x1f\x7f]*$")]] = None
    assay: Optional[Annotated[str, StringConstraints(max_length=200, pattern=r"^[^\x00-\x1f\x7f]*$")]] = None


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate, sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        n = s.execute(
            text("SELECT count(*) FROM project WHERE owner_id = :uid"), {"uid": sc.user.id}
        ).scalar_one()
        if n >= MAX_PROJECTS_PER_USER:
            raise HTTPException(status_code=409, detail="project limit reached")
        row = (
            s.execute(
                text(
                    "INSERT INTO project (owner_id, name) VALUES (:uid, :name) RETURNING id, name, created_at"
                ),
                {"uid": sc.user.id, "name": body.name},
            )
            .mappings()
            .one()
        )
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "compound_count": 0,
        "max_compounds": MAX_COMPOUNDS,
    }


@router.get("/projects")
def list_projects(sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        rows = (
            s.execute(
                text(
                    "SELECT p.id, p.name, p.created_at, (SELECT count(*) FROM compound c WHERE c.project_id = p.id AND c.owner_id = p.owner_id) AS n "
                    "FROM project p WHERE p.owner_id = :uid ORDER BY p.created_at DESC, p.id LIMIT 200"
                ),
                {"uid": sc.user.id},
            )
            .mappings()
            .all()
        )
    return {
        "projects": [
            {"id": r["id"], "name": r["name"], "created_at": r["created_at"], "compound_count": r["n"]}
            for r in rows
        ]
    }


@router.get("/projects/{project_id}")
def get_project(project_id: uuid.UUID, sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        proj = require_project(s, sc.user.id, project_id)
        comps = (
            s.execute(
                text(
                    "SELECT id, smiles, canonical_smiles, label, created_at FROM compound "
                    "WHERE project_id = :pid AND owner_id = :uid ORDER BY created_at, id"
                ),
                {"pid": project_id, "uid": sc.user.id},
            )
            .mappings()
            .all()
        )
        latest = latest_predictions(s, sc.user.id, project_id)
        meas = (
            s.execute(
                text(
                    "SELECT m.id, m.compound_id, m.endpoint, m.value, m.unit, m.assay, m.created_at FROM measurement m "
                    "JOIN compound c ON c.id = m.compound_id AND c.owner_id = m.owner_id "
                    "WHERE c.project_id = :pid AND m.owner_id = :uid ORDER BY m.created_at, m.id"
                ),
                {"pid": project_id, "uid": sc.user.id},
            )
            .mappings()
            .all()
        )
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


@router.patch("/projects/{project_id}")
def rename_project(project_id: uuid.UUID, body: ProjectRename, sc: Scope = Depends(scope)) -> dict:
    """Rename a project (owner only; 404 for anyone else's)."""
    with sc.session() as s:
        row = (
            s.execute(
                text(
                    "UPDATE project SET name = :name WHERE id = :pid AND owner_id = :uid "
                    "RETURNING id, name, created_at, "
                    "(SELECT count(*) FROM compound c WHERE c.project_id = project.id AND c.owner_id = project.owner_id) AS n"
                ),
                {"name": body.name, "pid": project_id, "uid": sc.user.id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found()
        _audit(s, sc.user.id, "project_rename", f"project:{project_id}")
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"], "compound_count": row["n"]}


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: uuid.UUID, sc: Scope = Depends(scope)) -> Response:
    with sc.session() as s:
        res = s.execute(
            text("DELETE FROM project WHERE id = :pid AND owner_id = :uid"),
            {"pid": project_id, "uid": sc.user.id},
        )
        if res.rowcount == 0:
            raise not_found()
    return Response(status_code=204)


def _audit(s: Session, uid: uuid.UUID, action: str, resource: str) -> None:
    """Ids only, never names, structures or values (the audit table is append-only)."""
    s.execute(
        text("INSERT INTO audit_log (actor_id, action, resource) VALUES (:u, :a, :r)"),
        {"u": uid, "a": action, "r": resource},
    )


@router.delete("/projects/{project_id}/compounds/{compound_id}", status_code=204)
def delete_compound(project_id: uuid.UUID, compound_id: uuid.UUID, sc: Scope = Depends(scope)) -> Response:
    """Delete one compound with its predictions and measurements (cascade). Owner only; 404 otherwise."""
    with sc.session() as s:
        require_project(s, sc.user.id, project_id)
        res = s.execute(
            text("DELETE FROM compound WHERE id = :cid AND project_id = :pid AND owner_id = :uid"),
            {"cid": compound_id, "pid": project_id, "uid": sc.user.id},
        )
        if res.rowcount == 0:
            raise not_found()
        _audit(s, sc.user.id, "compound_delete", f"compound:{compound_id}")
    return Response(status_code=204)


@router.delete("/projects/{project_id}/measurements/{measurement_id}", status_code=204)
def delete_measurement(
    project_id: uuid.UUID, measurement_id: uuid.UUID, sc: Scope = Depends(scope)
) -> Response:
    """Delete one measurement of a compound in this project. Owner only; 404 otherwise."""
    with sc.session() as s:
        require_project(s, sc.user.id, project_id)
        res = s.execute(
            text(
                "DELETE FROM measurement m USING compound c WHERE m.id = :mid AND m.owner_id = :uid "
                "AND c.id = m.compound_id AND c.owner_id = m.owner_id AND c.project_id = :pid"
            ),
            {"mid": measurement_id, "pid": project_id, "uid": sc.user.id},
        )
        if res.rowcount == 0:
            raise not_found()
        _audit(s, sc.user.id, "measurement_delete", f"measurement:{measurement_id}")
    return Response(status_code=204)


@router.delete("/projects/{project_id}/notes/{note_id}", status_code=204)
def delete_note(project_id: uuid.UUID, note_id: uuid.UUID, sc: Scope = Depends(scope)) -> Response:
    """Delete one note of this project. Owner only; 404 otherwise."""
    with sc.session() as s:
        require_project(s, sc.user.id, project_id)
        res = s.execute(
            text("DELETE FROM note WHERE id = :nid AND project_id = :pid AND owner_id = :uid"),
            {"nid": note_id, "pid": project_id, "uid": sc.user.id},
        )
        if res.rowcount == 0:
            raise not_found()
        _audit(s, sc.user.id, "note_delete", f"note:{note_id}")
    return Response(status_code=204)


def _note(r) -> dict:
    return {"id": r["id"], "body": r["body"], "created_at": r["created_at"]}


@router.get("/projects/{project_id}/notes")
def list_notes(project_id: uuid.UUID, sc: Scope = Depends(scope)) -> dict:
    """The caller's notes on this project, oldest first. Owner only; 404 for anyone else's project."""
    with sc.session() as s:
        require_project(s, sc.user.id, project_id)
        rows = (
            s.execute(
                text(
                    "SELECT id, body, created_at FROM note WHERE project_id = :pid AND owner_id = :uid "
                    "ORDER BY created_at, id LIMIT :lim"
                ),
                {"pid": project_id, "uid": sc.user.id, "lim": MAX_NOTES_PER_PROJECT},
            )
            .mappings()
            .all()
        )
    return {
        "notes": [_note(r) for r in rows],
        "max_notes": MAX_NOTES_PER_PROJECT,
        "max_chars": MAX_NOTE_CHARS,
    }


@router.post("/projects/{project_id}/notes", status_code=201)
def create_note(project_id: uuid.UUID, body: NoteIn, sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        require_project(s, sc.user.id, project_id)
        n = s.execute(
            text("SELECT count(*) FROM note WHERE project_id = :pid AND owner_id = :uid"),
            {"pid": project_id, "uid": sc.user.id},
        ).scalar_one()
        if n >= MAX_NOTES_PER_PROJECT:
            raise HTTPException(status_code=409, detail="note limit reached")
        row = (
            s.execute(
                text(
                    "INSERT INTO note (owner_id, project_id, body) VALUES (:uid, :pid, :body) "
                    "RETURNING id, body, created_at"
                ),
                {"uid": sc.user.id, "pid": project_id, "body": body.body},
            )
            .mappings()
            .one()
        )
        _audit(s, sc.user.id, "note_create", f"note:{row['id']}")
    return _note(row)


@router.patch("/projects/{project_id}/notes/{note_id}")
def update_note(project_id: uuid.UUID, note_id: uuid.UUID, body: NoteIn, sc: Scope = Depends(scope)) -> dict:
    """Replace the text of one note. Owner only; 404 otherwise (same answer for a missing and a foreign note)."""
    with sc.session() as s:
        require_project(s, sc.user.id, project_id)
        row = (
            s.execute(
                text(
                    "UPDATE note SET body = :body WHERE id = :nid AND project_id = :pid AND owner_id = :uid "
                    "RETURNING id, body, created_at"
                ),
                {"body": body.body, "nid": note_id, "pid": project_id, "uid": sc.user.id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found()
        _audit(s, sc.user.id, "note_update", f"note:{note_id}")
    return _note(row)


@router.post("/projects/{project_id}/measurements", status_code=201)
def add_measurement(project_id: uuid.UUID, body: MeasurementCreate, sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        require_project(s, sc.user.id, project_id)
        require_compound(s, sc.user.id, project_id, body.compound_id)
        row = (
            s.execute(
                text(
                    "INSERT INTO measurement (owner_id, compound_id, endpoint, value, unit, assay) "
                    "VALUES (:uid, :cid, :ep, :v, :unit, :assay) RETURNING id, created_at"
                ),
                {
                    "uid": sc.user.id,
                    "cid": body.compound_id,
                    "ep": body.endpoint,
                    "v": body.value,
                    "unit": body.unit,
                    "assay": body.assay,
                },
            )
            .mappings()
            .one()
        )
    return {
        "id": row["id"],
        "compound_id": body.compound_id,
        "endpoint": body.endpoint,
        "value": body.value,
        "unit": body.unit,
        "assay": body.assay,
        "label": "measured",
        "created_at": row["created_at"],
    }


@router.get("/projects/{project_id}/ranking")
def get_ranking_config(project_id: uuid.UUID, sc: Scope = Depends(scope)) -> dict:
    """The persisted ranking config (null if none was saved yet)."""
    with sc.session() as s:
        require_project(s, sc.user.id, project_id)
        cfg = s.execute(
            text("SELECT ranking_config FROM project WHERE id = :pid AND owner_id = :uid"),
            {"pid": project_id, "uid": sc.user.id},
        ).scalar_one()
    return {"config": cfg, "persisted": cfg is not None}


@router.put("/projects/{project_id}/ranking")
def put_ranking(
    project_id: uuid.UUID,
    config: RankingConfig,
    sc: Scope = Depends(scope),
    registry: ModelRegistry = Depends(get_registry),
) -> dict:
    """Validate and persist the ranking config (project.ranking_config) and return the ranking computed from the
    latest stored predictions. A config that cannot be ranked (422) is not persisted. Predictions made with a
    model version other than the current one are flagged in `stale_predictions` / `stale_model_version`."""
    with sc.session() as s:
        require_project(s, sc.user.id, project_id)
        try:
            ranked, stale = rank_project(s, registry, sc.user.id, project_id, config)
        except RankingError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        s.execute(
            text("UPDATE project SET ranking_config = CAST(:c AS jsonb) WHERE id = :pid AND owner_id = :uid"),
            {"c": config.model_dump_json(), "pid": project_id, "uid": sc.user.id},
        )
    stale_by_c: dict[str, list[str]] = {}
    for x in stale:
        stale_by_c.setdefault(x["compound_id"], []).append(x["endpoint"])
    return {
        "config": config.model_dump(mode="json"),
        "persisted": True,
        "formula": "R_j = (1 - lam) S_j + lam C_j",
        "stale_model_version": bool(stale),
        "stale_predictions": stale,
        "ranking": [
            {**r.model_dump(mode="json"), "stale_endpoints": stale_by_c.get(r.compound_id, [])}
            for r in ranked
        ],
    }
