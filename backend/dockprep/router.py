# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310
"""POST /dockprep/ligands, /dockprep/proteins, /dockprep/sites. Stateless: nothing is stored; the result is returned
as JSON (files base64) or as a zip with ?bundle=zip. Auth is required on every route; rate limits belong to Accounts."""
from __future__ import annotations

import base64
import binascii
import logging
from typing import Literal

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import configs
from .auth import current_user
from .bundle import change_log_text, files_b64, make_zip
from .errors import DockprepError
from .fetch import RcsbFetcher, StructureFetcher, urllib_http_get, validate_pdb_id
from .limits import MAX_BODY_BYTES, MAX_UPLOAD_BYTES, sanitize_name
from .sandbox import run_sandboxed

log = logging.getLogger("metis.dockprep")
router = APIRouter(prefix="/dockprep", tags=["dockprep"])


def fetcher_provider():
    """Dependency returning a zero-arg factory for the fetcher (overridable in tests)."""
    return get_fetcher


def get_fetcher() -> StructureFetcher:
    import os

    if os.environ.get("METIS_DOCKPREP_FETCH") != "1":
        raise DockprepError("fetch_disabled", "Fetching by PDB ID is not enabled on this server; upload the file instead.", 503)
    return RcsbFetcher(urllib_http_get)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LigandReq(_Base):
    smiles: str | None = Field(default=None, max_length=2000)
    sdf: str | None = Field(default=None, max_length=2_000_000)
    name: str = Field(default="ligand", max_length=80)
    ph: float = 7.4
    n_conformers: int = Field(default=10, ge=1, le=50)
    seed: int = 42
    all_conformers_sdf: bool = False


class _StructReq(_Base):
    pdb_id: str | None = Field(default=None, max_length=8)
    file_b64: str | None = Field(default=None, max_length=MAX_BODY_BYTES)
    filename: str | None = Field(default=None, max_length=200)   # sanitized and never used as a path


class ProteinReq(_StructReq):
    chains: list[str] | None = Field(default=None, max_length=50)
    remove_waters: bool = True
    remove_heteroatoms: bool = True
    keep_hets: list[str] = Field(default_factory=list, max_length=50)
    keep_metals: bool = True
    add_hydrogens: bool = True
    ph: float = 7.4


class BoxReq(_Base):
    center: list[float] = Field(min_length=3, max_length=3)
    size: list[float] = Field(min_length=3, max_length=3)


class SitesReq(_StructReq):
    modes: list[Literal["residues", "ligand", "box", "detect", "literature"]] = Field(min_length=1, max_length=5)
    residues: list[str] | None = Field(default=None, max_length=200)
    ligand_id: str | None = Field(default=None, max_length=20)
    box: BoxReq | None = None
    padding: float = 4.0
    chains: list[str] | None = Field(default=None, max_length=50)
    engine: Literal["vina", "gnina"] = "vina"
    config_site: str | None = Field(default=None, max_length=12)
    exhaustiveness: int = 8
    num_modes: int = 9
    cnn_scoring: bool = False
    receptor_file: str = "receptor.pdbqt"
    ligand_file: str | None = None


async def _read_model(request: Request, model: type[BaseModel]):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        raise DockprepError("too_large", "Request body is too large.", 413)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise DockprepError("too_large", "Request body is too large.", 413)
    try:
        return model.model_validate_json(bytes(body))
    except ValidationError:
        raise DockprepError("bad_request", "Request body is invalid.", 422) from None


def _structure_bytes(req: _StructReq, fetcher_dep) -> tuple[bytes, str | None]:
    if bool(req.pdb_id) == bool(req.file_b64):
        raise DockprepError("bad_request", "Provide exactly one of pdb_id or file_b64.", 422)
    if req.pdb_id:
        pid = validate_pdb_id(req.pdb_id)
        return fetcher_dep().fetch_pdb(pid), pid
    try:
        raw = base64.b64decode(req.file_b64 or "", validate=True)
    except (binascii.Error, ValueError):
        raise DockprepError("bad_request", "file_b64 is not valid base64.", 422) from None
    if len(raw) > MAX_UPLOAD_BYTES:
        raise DockprepError("too_large", "Structure file is too large.", 413)
    if not raw:
        raise DockprepError("bad_request", "Structure file is empty.", 422)
    return raw, None


def _respond(res: dict, bundle: str | None, extra: dict | None = None) -> Response | dict:
    extra = extra or {}
    if bundle == "zip":
        z = make_zip(res["files"], res["change_log"], res["report"], extra.get("files"))
        return Response(z, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="metis_dockprep.zip"'})
    files = {**res["files"], **extra.get("files", {})}
    return {"files": files_b64(files), "change_log": res["change_log"], "change_log_text": change_log_text(res["change_log"]),
            "report": res["report"], **{k: v for k, v in extra.items() if k != "files"}}


def _handle(e: DockprepError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message})


@router.post("/ligands")
async def post_ligands(request: Request, bundle: Literal["zip"] | None = None, user=Depends(current_user)):
    try:
        req = await _read_model(request, LigandReq)
        res = await anyio.to_thread.run_sync(run_sandboxed, "ligand", req.model_dump())
        return _respond(res, bundle)
    except DockprepError as e:
        raise _handle(e) from None


@router.post("/proteins")
async def post_proteins(request: Request, bundle: Literal["zip"] | None = None, user=Depends(current_user),
                        fetcher=Depends(fetcher_provider)):
    try:
        req = await _read_model(request, ProteinReq)
        raw, pid = await anyio.to_thread.run_sync(_structure_bytes, req, fetcher)
        payload = {**req.model_dump(exclude={"file_b64", "filename", "pdb_id"}), "data_b64": base64.b64encode(raw).decode(), "pdb_id": pid}
        res = await anyio.to_thread.run_sync(run_sandboxed, "protein", payload)
        return _respond(res, bundle)
    except DockprepError as e:
        raise _handle(e) from None


@router.post("/sites")
async def post_sites(request: Request, bundle: Literal["zip"] | None = None, user=Depends(current_user),
                     fetcher=Depends(fetcher_provider)):
    try:
        req = await _read_model(request, SitesReq)
        raw, _pid = await anyio.to_thread.run_sync(_structure_bytes, req, fetcher)
        payload = {"data_b64": base64.b64encode(raw).decode(), "modes": req.modes, "residues": req.residues, "ligand_id": req.ligand_id,
                   "box": req.box.model_dump() if req.box else None, "padding": req.padding, "chains": req.chains}
        res = await anyio.to_thread.run_sync(run_sandboxed, "sites", payload)
        sites = res["sites"]
        cfg_files: dict[str, str] = {}
        chosen = None
        if sites:
            chosen = next((s for s in sites if s["id"] == req.config_site), sites[0]) if req.config_site else sites[0]
            kw = dict(exhaustiveness=req.exhaustiveness, num_modes=req.num_modes, receptor=sanitize_name(req.receptor_file, "receptor.pdbqt", 80))
            if req.engine == "vina":
                cfg_files["vina.conf"] = configs.vina_config(chosen["center"], chosen["size"], ligand=sanitize_name(req.ligand_file or "ligand.pdbqt", "ligand.pdbqt", 80), **kw)
            else:
                cfg_files["gnina.conf"] = configs.gnina_config(chosen["center"], chosen["size"], ligand=sanitize_name(req.ligand_file or "ligand.sdf", "ligand.sdf", 80),
                                                              cnn_scoring=req.cnn_scoring, **kw)
            cfg_files["box.txt"] = (f"center_x = {chosen['center'][0]:.3f}\ncenter_y = {chosen['center'][1]:.3f}\ncenter_z = {chosen['center'][2]:.3f}\n"
                                    f"size_x = {chosen['size'][0]:.3f}\nsize_y = {chosen['size'][1]:.3f}\nsize_z = {chosen['size'][2]:.3f}\n")
        site_log = [{"step": "site", "action": "site defined", "target": s["id"], "detail": f"{s['label']} via {s['method']}",
                     "params": {"center": s["center"], "size": s["size"]}} for s in sites]
        out = {"files": cfg_files, "change_log": site_log, "report": {"structure": res["structure"], "sites": sites, "notes": res["notes"], "detection": res["detection"],
                                                                        "config_site": chosen["id"] if chosen else None, "engine": req.engine}}
        return _respond(out, bundle, {"sites": sites, "notes": res["notes"]})
    except DockprepError as e:
        raise _handle(e) from None
