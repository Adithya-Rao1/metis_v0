"""Compound ingest, conformer and depiction routes.

All structure parsing runs in the sandbox worker subprocess. No structure content is logged or put in errors:
errors refer to record positions only.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.compounds import sandbox
from backend.jobs.deps import Scope, limit_body, require_compound, require_project, scope
from backend.schemas.contracts import MAX_COMPOUNDS, Strict

log = logging.getLogger("metis.compounds")
router = APIRouter(tags=["compounds"])

MAX_CONTENT_CHARS = 1_000_000


class AddCompounds(Strict):
    format: Literal["smiles", "csv", "sdf", "molblock"] = "smiles"
    smiles: Optional[list[str]] = Field(default=None, max_length=MAX_COMPOUNDS)
    content: Optional[str] = Field(default=None, max_length=MAX_CONTENT_CHARS)


def _run(request: dict) -> dict:
    try:
        return sandbox.run(request)
    except sandbox.WorkerBusy:
        raise HTTPException(
            status_code=503,
            detail="structure processing is busy; try again shortly",
            headers={"Retry-After": "5"},
        ) from None
    except sandbox.WorkerError:
        log.warning("structure worker failed")
        raise HTTPException(status_code=503, detail="structure processing failed or timed out") from None


@router.post("/projects/{project_id}/compounds", status_code=201, dependencies=[Depends(limit_body)])
def add_compounds(project_id: uuid.UUID, body: AddCompounds, sc: Scope = Depends(scope)) -> dict:
    if body.format == "smiles" and (not body.smiles or body.content is not None):
        raise HTTPException(status_code=422, detail="format 'smiles' needs a 'smiles' list and no content")
    if body.format != "smiles" and (body.smiles is not None or not body.content):
        raise HTTPException(
            status_code=422, detail=f"format '{body.format}' needs 'content' and no smiles list"
        )
    with sc.session() as s:  # ownership check before spending worker time
        require_project(s, sc.user.id, project_id)
    out = _run(
        {
            "op": "standardize",
            "format": body.format,
            "smiles": body.smiles,
            "content": body.content,
            "max_records": MAX_COMPOUNDS,
        }
    )
    err = out.get("error")
    if err == "too_many":
        raise HTTPException(status_code=422, detail=f"at most {MAX_COMPOUNDS} compounds per project")
    if err == "format":
        raise HTTPException(
            status_code=422, detail=str(out.get("detail") or "input is not in the expected format")
        )
    if err:
        raise HTTPException(status_code=503, detail="structure processing failed")
    records = out["records"]
    problems = [{"record": i + 1, "error": r["error"]} for i, r in enumerate(records) if not r["ok"]]
    if problems:
        raise HTTPException(
            status_code=422,
            detail={"message": "some records were rejected; nothing was added", "problems": problems},
        )
    canon = [r["canonical"] for r in records]
    if len(set(canon)) != len(canon):
        raise HTTPException(status_code=422, detail="duplicate structures in this submission")
    try:
        with sc.session() as s:
            require_project(s, sc.user.id, project_id)
            s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": str(project_id)})
            existing = {
                r[0]
                for r in s.execute(
                    text("SELECT canonical_smiles FROM compound WHERE project_id = :pid AND owner_id = :uid"),
                    {"pid": project_id, "uid": sc.user.id},
                )
            }
            if len(existing) + len(records) > MAX_COMPOUNDS:
                raise HTTPException(status_code=422, detail=f"at most {MAX_COMPOUNDS} compounds per project")
            if existing & set(canon):
                raise HTTPException(
                    status_code=422, detail="a submitted structure is already in this project"
                )
            created = []
            for r in records:
                row = (
                    s.execute(
                        text(
                            "INSERT INTO compound (owner_id, project_id, smiles, canonical_smiles, label, created_at) "
                            "VALUES (:uid, :pid, :smi, :can, :label, clock_timestamp()) RETURNING id, smiles, canonical_smiles, label"
                        ),
                        {
                            "uid": sc.user.id,
                            "pid": project_id,
                            "smi": r["canonical"],
                            "can": r["canonical"],
                            "label": r.get("label"),
                        },
                    )
                    .mappings()
                    .one()
                )
                created.append(dict(row))
    except DBAPIError as e:  # the DB cap trigger is the second layer
        if getattr(e.orig, "sqlstate", None) == "23514":
            raise HTTPException(
                status_code=422, detail=f"at most {MAX_COMPOUNDS} compounds per project"
            ) from None
        raise HTTPException(status_code=400, detail="could not store compounds") from None
    return {"compounds": created}


def _canonical(sc: Scope, project_id: uuid.UUID, compound_id: uuid.UUID) -> dict:
    with sc.session() as s:
        require_project(s, sc.user.id, project_id)
        return require_compound(s, sc.user.id, project_id, compound_id)


@router.get("/projects/{project_id}/compounds/{compound_id}/conformer")
def conformer(project_id: uuid.UUID, compound_id: uuid.UUID, sc: Scope = Depends(scope)) -> dict:
    c = _canonical(sc, project_id, compound_id)
    out = _run({"op": "conformers", "smiles": [c["canonical_smiles"]]})
    conf = (out.get("conformers") or [{"ok": False}])[0]
    if not conf.get("ok"):
        raise HTTPException(status_code=422, detail="3D conformer could not be generated for this structure")
    return {
        "compound_id": c["id"],
        "molblock": conf["molblock"],
        "method": conf["method"],
        "label": "computed",
        "note": "Computed conformer (not experimental); one low-energy embedding, not a conformer ensemble.",
        "persisted": False,
    }


_SVG_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "X-Content-Type-Options": "nosniff",
}
Highlight = Annotated[Optional[list[int]], Query(max_length=200)]


def _depict(sc: Scope, project_id: uuid.UUID, compound_id: uuid.UUID, highlight: Optional[list[int]]) -> dict:
    c = _canonical(sc, project_id, compound_id)
    hl = [i for i in (highlight or []) if 0 <= i < 1000]
    out = _run({"op": "depict", "smiles": c["canonical_smiles"], "highlight": hl})
    if not out.get("ok"):
        raise HTTPException(status_code=422, detail="depiction failed")
    return out


@router.get("/projects/{project_id}/compounds/{compound_id}/depiction.svg")
def depiction(
    project_id: uuid.UUID, compound_id: uuid.UUID, highlight: Highlight = None, sc: Scope = Depends(scope)
) -> Response:
    out = _depict(sc, project_id, compound_id, highlight)
    return Response(content=out["svg"], media_type="image/svg+xml", headers=_SVG_HEADERS)


@router.get("/projects/{project_id}/compounds/{compound_id}/atoms")
def atom_map(project_id: uuid.UUID, compound_id: uuid.UUID, sc: Scope = Depends(scope)) -> dict:
    """Atom index -> element and drawing coordinates (indices refer to the canonical SMILES atom order)."""
    out = _depict(sc, project_id, compound_id, None)
    return {"compound_id": compound_id, "width": 400, "height": 300, "atoms": out["atoms"]}
