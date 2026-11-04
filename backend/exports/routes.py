"""GET /projects/{id}/export?format=pdf|sdf|csv (owner only; disclaimer in every format)."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text

from backend.compounds import sandbox
from backend.exports.formats import ExportCompound, to_csv, to_pdf, to_sdf
from backend.jobs.deps import Scope, require_project, scope
from backend.ranking.service import latest_predictions

router = APIRouter(tags=["exports"])


@router.get("/projects/{project_id}/export")
def export_project(project_id: uuid.UUID, format: Literal["pdf", "sdf", "csv"] = Query(...), sc: Scope = Depends(scope)) -> Response:
    with sc.session() as s:
        proj = require_project(s, sc.user.id, project_id)
        comps = s.execute(
            text("SELECT id, label, canonical_smiles FROM compound WHERE project_id = :pid AND owner_id = :uid ORDER BY created_at, id"),
            {"pid": project_id, "uid": sc.user.id},
        ).mappings().all()
        latest = latest_predictions(s, sc.user.id, project_id)
        meas = s.execute(
            text(
                "SELECT m.compound_id, m.endpoint, m.value FROM measurement m JOIN compound c ON c.id = m.compound_id AND c.owner_id = m.owner_id "
                "WHERE c.project_id = :pid AND m.owner_id = :uid ORDER BY m.created_at, m.id"
            ),
            {"pid": project_id, "uid": sc.user.id},
        ).all()
    items = [
        ExportCompound(i + 1, c["label"], c["canonical_smiles"], {e: p for e, (_, p) in latest.get(c["id"], {}).items()})
        for i, c in enumerate(comps)
    ]
    pos = {c["id"]: it for c, it in zip(comps, items)}
    for cid, ep, val in meas:
        if cid in pos:
            pos[cid].measured.setdefault(ep, []).append(val)
    endpoints = sorted({e for it in items for e in it.predictions} | {e for it in items for e in it.measured})
    if format == "csv":
        body: str | bytes = to_csv(proj["name"], items, endpoints)
        media, ext = "text/csv; charset=utf-8", "csv"
    elif format == "pdf":
        body = to_pdf(proj["name"], items, endpoints)
        media, ext = "application/pdf", "pdf"
    else:
        if items:
            try:
                out = sandbox.run({"op": "conformers", "smiles": [it.canonical_smiles for it in items]})
            except sandbox.WorkerError:
                raise HTTPException(status_code=503, detail="structure processing failed or timed out") from None
            confs = out.get("conformers") or []
            for it, cf in zip(items, confs):
                if cf.get("ok"):
                    it.molblock, it.conformer_method = cf["molblock"], cf["method"]
            missing = [it for it in items if it.molblock is None]
            if missing:  # fall back to honest 2D coordinates rather than failing the export
                from rdkit import Chem
                from rdkit.Chem import rdDepictor

                for it in missing:
                    m = Chem.MolFromSmiles(it.canonical_smiles)
                    rdDepictor.Compute2DCoords(m)
                    it.molblock = Chem.MolToMolBlock(m)
        body = to_sdf(proj["name"], items)
        media, ext = "chemical/x-mdl-sdfile; charset=utf-8", "sdf"
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="metis_export.{ext}"', "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )
