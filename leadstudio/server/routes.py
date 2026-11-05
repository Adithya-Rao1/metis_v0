"""Metis Lead Studio (Beta) HTTP API, mounted by `backend.main` under `/lead`.

  /lead/api/*       health, targets, presets, runs (+ SSE events), molecule validation, poses, manual mode, data files
  /lead/library/*   saved runs and saved manual-edit sessions   (database, owner-only)
  /lead/profiles/*  scoring profiles                              (database, owner-only)

AUTHENTICATION is the Metis session: every route depends on `backend.accounts.deps.current_user` (session cookie;
CSRF double-submit and the current terms version on every mutation). Anonymous -> 401. Objects of other users ->
404, never 403. Rate limits use the shared database limiter. See docs/OPERATIONS.md for the env settings.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import secrets
import threading
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from backend.accounts import security
from backend.accounts.deps import rate_limit
from backend.jobs.deps import Scope, current_user, not_found, scope
from leadstudio.adapt.dataset import MAX_TRANSITIONS, MIN_VALID_STEPS
from leadstudio.adapt.jobs import QueueFull
from leadstudio.profiles import MAX_SMARTS, ProfileBody, check_smarts
from leadstudio.schema import (
    SCHEMA_VERSION,
    Descriptors,
    Event,
    ManualPreview,
    ManualPreviewRequest,
    ManualSession,
    ManualStep,
    PoseRequest,
    PoseResult,
    RunConfig,
    RunCreated,
    ScoreResult,
    ValidateRequest,
    ValidateResponse,
)
from leadstudio.server import molio, stages, store
from leadstudio.server.core import Rejected
from leadstudio.server.manual import export_jsonl
from leadstudio.server.state import LeadState, get_state

log = logging.getLogger("leadstudio.server")

MAX_SAVED_PAYLOAD = 1_500_000  # bytes of JSON for one saved run
MAX_MANUAL_STEPS = 500
ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"  # ids of in-memory runs / sessions / pose jobs (token_urlsafe)
OBJECTIVE_KEYS = frozenset(Descriptors.model_fields)

# Every route below needs a signed-in Metis user (also those that do not otherwise use the identity).
router = APIRouter(prefix="/lead", tags=["lead-studio"], dependencies=[Depends(current_user)])
api = APIRouter(prefix="/api")
library = APIRouter(prefix="/library")
profiles = APIRouter(prefix="/profiles")


def throttle(sc: Scope, route: str, limit: int, window_s: int = 60) -> None:
    """Per-user fixed-window limit in the shared database limiter (429 + Retry-After when exceeded)."""
    rate_limit(sc.factory, security.rl_key("user", f"lead_{route}", str(sc.user.id)), limit, window_s)


# ====================================================================================================== api
@api.get("/health")
def health(st: LeadState = Depends(get_state)) -> dict:
    s = st.settings
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "source": st.source,
        "engine_source": st.source,
        "force_mock": s.force_mock,
        "components": st.components,
        "details": st.details,
        "limits": {
            "max_concurrent_runs": s.max_concurrent,
            "max_queue": s.max_queue,
            "runs_per_user": s.per_user_active,
            "run_timeout_s": s.run_timeout,
            "max_generations": s.max_generations,
            "max_population": s.max_population,
            "runs_per_minute_per_user": s.runs_per_minute,
            "max_heavy_atoms": molio.MAX_HEAVY_ATOMS,
            "max_smiles_chars": molio.MAX_SMILES_CHARS,
        },
    }


@api.get("/presets")
def presets(st: LeadState = Depends(get_state)) -> list:
    return st.presets


@api.post("/molecules/validate", response_model=ValidateResponse)
def validate(
    req: ValidateRequest, sc: Scope = Depends(scope), st: LeadState = Depends(get_state)
) -> ValidateResponse:
    throttle(sc, "validate", st.settings.validates_per_minute)
    with st.heavy:
        try:
            mol = molio.parse_molecule(req.smiles, req.molfile)
        except molio.InvalidMolecule as exc:
            return ValidateResponse(ok=False, error=str(exc), source=st.source)
        smi = molio.canonical(mol)
        block, _provider = molio.molblock_3d(smi)
        if block is None:
            return ValidateResponse(
                ok=False,
                canonical_smiles=smi,
                heavy_atoms=mol.GetNumHeavyAtoms(),
                source=st.source,
                error="Could not generate a 3D conformer for this molecule",
            )
        score: ScoreResult | None = None
        if st.scorer is not None:
            try:
                score = st.scorer.score(smi, RunConfig(seed_smiles=smi))
            except Exception as exc:
                log.warning("seed scoring failed: %s", type(exc).__name__)
        return ValidateResponse(
            ok=True,
            canonical_smiles=smi,
            heavy_atoms=mol.GetNumHeavyAtoms(),
            molblock=block,
            score=score,
            source=st.source,
        )


def _profile_wrapper(sc: Scope, cfg: RunConfig):
    """Scorer wrapper for the caller's own saved profile (404 if it is not theirs), else None."""
    if cfg.profile_id is None:
        return None
    if cfg.stage != "pharmacology":
        raise HTTPException(422, "profile_id applies to the pharmacology stage only")
    try:
        pid = uuid.UUID(cfg.profile_id)
    except ValueError:
        raise HTTPException(422, "profile_id is not valid") from None
    with sc.session() as s:
        row = store.get_profile(s, sc.user.id, pid)
    if row is None:
        raise not_found()
    try:
        prof = ProfileBody.model_validate(row["body"])
    except ValidationError:
        raise HTTPException(422, "the saved profile is no longer valid; edit and save it again") from None
    from leadstudio.profile_scorer import ProfileScorer

    return lambda base: ProfileScorer(base, prof)


@api.post("/runs", response_model=RunCreated)
def create_run(cfg: RunConfig, sc: Scope = Depends(scope), st: LeadState = Depends(get_state)) -> RunCreated:
    s = st.settings
    throttle(sc, "runs", s.runs_per_minute)
    bad = sorted(set(cfg.objectives) - OBJECTIVE_KEYS)
    if bad:
        raise HTTPException(422, "unknown objective name")
    if cfg.adapter_id is not None:
        raise HTTPException(422, "policy adapters are not available in this service")
    try:
        mol = molio.parse_molecule(cfg.seed_smiles)
    except molio.InvalidMolecule as exc:
        raise HTTPException(422, str(exc)) from None
    n = mol.GetNumHeavyAtoms()
    if any(i < 0 or i >= n for i in cfg.locked_atoms):
        raise HTTPException(422, "locked_atoms index out of range")
    cfg = stages.prepare_config(cfg, st.svc, st.engine is not None)
    wrap = _profile_wrapper(sc, cfg)
    cfg = cfg.model_copy(
        update={
            "seed_smiles": molio.canonical(mol),
            "generations": max(1, min(cfg.generations, s.max_generations)),
            "population_size": max(2, min(cfg.population_size, s.max_population)),
            "policy_guidance": max(0.0, min(cfg.policy_guidance, 1.0)),
            "locked_atoms": sorted(set(cfg.locked_atoms)),
            "use_reactions": cfg.use_reactions if st.engine is not None else False,
            "seed_molblock": None,  # the engine builds its own 3D seed; a client-supplied block is never used
        }
    )
    run = st.mgr.submit(sc.user.id, cfg, secrets.token_urlsafe(9), scorer_wrap=wrap)
    log.info(
        "run %s created source=%s heavy_atoms=%d gens=%d pop=%d",
        run.run_id[:6],
        run.source,
        n,
        cfg.generations,
        cfg.population_size,
    )
    return RunCreated(run_id=run.run_id, source="mock" if run.source == "mock" else "live")


@api.delete("/runs/{run_id}")
def cancel_run(
    run_id: str = Path(pattern=ID_PATTERN), user=Depends(current_user), st: LeadState = Depends(get_state)
) -> dict:
    run = st.mgr.get(run_id, user.id)
    if run is None:
        raise HTTPException(404, "unknown run")
    return {"cancelled": st.mgr.cancel(run)}


@api.get("/runs/{run_id}/events")
async def run_events(
    request: Request,
    run_id: str = Path(pattern=ID_PATTERN),
    user=Depends(current_user),
    st: LeadState = Depends(get_state),
) -> StreamingResponse:
    run = st.mgr.get(run_id, user.id)
    if run is None:
        raise HTTPException(404, "unknown run")
    if not st.mgr.open_stream(user.id):
        raise Rejected(429, "Too many open event streams", 5)
    last = request.headers.get("last-event-id")
    start = int(last) + 1 if last and last.isdigit() and len(last) < 9 else 0
    heartbeat = st.settings.heartbeat

    async def stream():
        i, idle = start, time.monotonic()
        try:
            yield "retry: 3000\n\n"
            while True:
                if i < len(run.events):
                    ev = run.events[i]
                    if run.pace and i > start:
                        await asyncio.sleep(run.pace)
                    yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev, separators=(',', ':'))}\n\n"
                    i, idle = i + 1, time.monotonic()
                    continue
                if run.finished:
                    return
                if await request.is_disconnected():
                    return
                if time.monotonic() - idle >= heartbeat:
                    yield ": heartbeat\n\n"
                    idle = time.monotonic()
                await asyncio.sleep(0.1)
        finally:
            st.mgr.close_stream(user.id)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@api.get("/targets")
def targets(st: LeadState = Depends(get_state)) -> list:
    return [t.model_dump(mode="json") for t in st.svc.list_targets()]


@api.get("/targets/{target_id}")
def target(target_id: str = Path(max_length=64), st: LeadState = Depends(get_state)) -> dict:
    t = st.svc.get_target(target_id)
    if t is None:
        raise HTTPException(404, "unknown target")
    return t.model_dump(mode="json")


@api.get("/data/{path:path}")
def data(path: str = Path(max_length=200)) -> FileResponse:
    p = stages.resolve_data_path(path)
    if p is None:
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type=stages.DATA_EXT[p.suffix.lower()])


@api.post("/poses", response_model=PoseResult)
def create_pose(
    req: PoseRequest, sc: Scope = Depends(scope), st: LeadState = Depends(get_state)
) -> PoseResult:
    throttle(sc, "poses_any", 120)
    if st.svc.get_target(req.target_id) is None:
        raise HTTPException(422, "unknown target_id")
    try:
        smi = molio.canonical(molio.parse_molecule(req.smiles))
    except molio.InvalidMolecule as exc:
        raise HTTPException(422, str(exc)) from None
    if not st.poses.precomputed_exists(req.target_id, smi):
        throttle(
            sc, "poses", 10
        )  # only requests that would reach the docking service are held to the strict limit
    return st.poses.submit(sc.user.id, req.target_id, smi)


@api.get("/poses/{job_id}", response_model=PoseResult)
def get_pose(
    job_id: str = Path(pattern=ID_PATTERN), user=Depends(current_user), st: LeadState = Depends(get_state)
) -> PoseResult:
    j = st.poses.get(job_id, user.id)
    if j is None:
        raise HTTPException(404, "unknown or expired pose job")
    return j


# ---------------------------------------------------------------------------------------------- manual mode
@api.get("/manual/applicable")
def applicable(
    smiles: str = Query(..., max_length=2000),
    locked: str = Query("", max_length=300),
    sc: Scope = Depends(scope),
    st: LeadState = Depends(get_state),
) -> list:
    throttle(sc, "applicable", 120)
    with st.heavy:
        return st.manual.applicable(smiles, locked)


@api.post("/manual/preview", response_model=ManualPreview)
def preview(
    req: ManualPreviewRequest, sc: Scope = Depends(scope), st: LeadState = Depends(get_state)
) -> ManualPreview:
    throttle(sc, "preview", 120)
    with st.heavy:
        return st.manual.preview(req)


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed_smiles: str = Field(max_length=2000)
    target_id: str | None = Field(default=None, max_length=64)


@api.post("/manual/sessions", response_model=ManualSession)
def create_session(
    req: SessionCreate, sc: Scope = Depends(scope), st: LeadState = Depends(get_state)
) -> ManualSession:
    throttle(sc, "msess", 20)
    return st.manual.create_session(sc.user.id, req.seed_smiles, req.target_id)


@api.post("/manual/sessions/{session_id}/steps", response_model=ManualSession)
def add_step(
    step: ManualStep,
    session_id: str = Path(pattern=ID_PATTERN),
    sc: Scope = Depends(scope),
    st: LeadState = Depends(get_state),
) -> ManualSession:
    throttle(sc, "mstep", 240)
    with st.heavy:
        return st.manual.add_step(sc.user.id, session_id, step)


@api.get("/manual/sessions/{session_id}/export")
def export(
    session_id: str = Path(pattern=ID_PATTERN), user=Depends(current_user), st: LeadState = Depends(get_state)
) -> Response:
    s = st.manual.sessions.get(session_id, user.id)
    if s is None:
        raise HTTPException(404, "unknown session")
    return Response(
        export_jsonl(s),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="manual_session.jsonl"'},
    )


# ================================================================================================= adapt
# "Adapt to my edits": fine-tunes a small policy on the caller's own logged manual edits. Needs the optional torch
# dependency; without it (or in mock mode) job creation answers 503. Jobs are owner-scoped, one active per user.
_adapt_lock = threading.Lock()


class AdaptJobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transitions: list[dict[str, Any]] | None = Field(default=None, max_length=MAX_TRANSITIONS)
    manual_session_id: uuid.UUID | None = None  # a session saved in the caller's library


def _adapt_manager(request: Request) -> Any:
    st = request.app.state
    mgr = getattr(st, "lead_adapt", None)
    if mgr is None:
        with _adapt_lock:
            mgr = getattr(st, "lead_adapt", None)
            if mgr is None:
                from leadstudio.adapt.jobs import JobManager

                def runner(transitions: Any, owner: str, progress: Any = None) -> dict:
                    from leadstudio.adapt import pipeline
                    from leadstudio.registry import get_registry

                    return pipeline.run(transitions, owner, registry=get_registry(), progress=progress)

                mgr = st.lead_adapt = JobManager(runner)
    return mgr


@api.post("/adapt/jobs", status_code=202)
def create_adapt_job(
    body: AdaptJobIn, request: Request, sc: Scope = Depends(scope), st: LeadState = Depends(get_state)
) -> dict:
    throttle(sc, "adapt", 5)
    if (body.transitions is None) == (body.manual_session_id is None):
        raise HTTPException(422, "Send exactly one of transitions or manual_session_id")
    if st.source != "live" or importlib.util.find_spec("torch") is None:
        raise Rejected(503, "Policy adaptation is not available on this server")
    if body.manual_session_id is not None:
        with sc.session() as s:
            m = store.get_manual(s, sc.user.id, body.manual_session_id)
        if m is None:
            raise not_found()
        trans = [
            {
                "parent_smiles": x["parent_smiles"],
                "edit_id": x["edit_id"],
                "site": list(x["site"] or []),
                "child_smiles": x["child_smiles"],
                "reward": float(x["reward_delta"] or 0.0),
            }
            for x in m["steps"]
        ]
    else:
        trans = body.transitions or []
    if len(trans) < MIN_VALID_STEPS:
        raise HTTPException(422, f"Need at least {MIN_VALID_STEPS} logged edits to adapt")
    mgr = _adapt_manager(request)
    owner = str(sc.user.id)
    if any(j["owner"] == owner and j["status"] in ("queued", "running") for j in list(mgr.jobs.values())):
        raise Rejected(429, "You already have an adaptation in progress", 30)
    try:
        return {"job_id": mgr.submit(owner, trans), "source": st.source}
    except QueueFull as exc:
        raise Rejected(429, str(exc), 30) from None


@api.get("/adapt/jobs/{job_id}")
def get_adapt_job(
    request: Request,
    job_id: str = Path(pattern=ID_PATTERN),
    user=Depends(current_user),
    st: LeadState = Depends(get_state),
) -> dict:
    j = _adapt_manager(request).get(job_id, str(user.id))
    if j is None:
        raise HTTPException(404, "unknown job")
    return {**j, "source": st.source}


# ================================================================================================= library
def _cap(n: int, limit: int, what: str) -> None:
    if n >= limit:
        raise HTTPException(409, f"Library full: at most {limit} {what}. Delete some first.")


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=120)
    stage: Literal["binding", "pharmacology"] | None = None
    target_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    events: list[Event] = Field(min_length=1, max_length=5000)


def _run_summary(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in ("id", "title", "stage", "target_id", "seed_smiles", "headline", "created_at")}


@library.get("/runs")
def list_runs(sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        rows = store.list_runs(s, sc.user.id)
    return {"runs": [_run_summary(r) for r in rows]}


@library.post("/runs", status_code=201)
def save_run(body: RunIn, sc: Scope = Depends(scope)) -> dict:
    throttle(sc, "lib_write", 30)
    events = [e.model_dump(mode="json") for e in body.events]
    for e in body.events:
        try:
            e.typed_payload()
        except ValidationError:
            raise HTTPException(422, "events do not match the run event schema") from None
    blob = json.dumps(events, separators=(",", ":"))
    if len(blob.encode("utf-8")) > MAX_SAVED_PAYLOAD:
        raise HTTPException(413, f"Run too large (max {MAX_SAVED_PAYLOAD // 1_000_000} MB)")
    start = next((e for e in events if e["type"] == "run_start"), None)
    end = next((e for e in reversed(events) if e["type"] == "run_end"), None)
    rc = start["payload"].get("config", {}) if start else {}
    seed = rc.get("seed_smiles") or (end or {}).get("payload", {}).get("seed_smiles")
    headline = (end or {}).get("payload", {}).get("headline")
    stage = body.stage or rc.get("stage") or "pharmacology"
    if stage not in ("binding", "pharmacology"):
        raise HTTPException(422, "only binding and pharmacology runs can be saved")
    target = body.target_id or rc.get("target_id")
    if target is not None and (not isinstance(target, str) or len(target) > 64):
        target = None
    title = (body.title or "").strip() or (headline or "Lead Studio run")
    title = "".join(ch for ch in title if ch.isprintable())[:120] or "Lead Studio run"
    with sc.session() as s:
        store.lock_owner(s, sc.user.id, "run")
        _cap(store.count_runs(s, sc.user.id), store.MAX_RUNS, "saved runs")
        row = store.insert_run(
            s,
            sc.user.id,
            title,
            stage,
            target,
            seed[:2000] if isinstance(seed, str) and seed else None,
            headline[:300] if isinstance(headline, str) else None,
            blob,
        )
    return _run_summary(row)


@library.get("/runs/{run_id}")
def get_run(run_id: uuid.UUID, sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        r = store.get_run(s, sc.user.id, run_id)
    if r is None:
        raise not_found()
    return {**_run_summary(r), "events": r["events"]}


@library.delete("/runs/{run_id}")
def delete_run(run_id: uuid.UUID, sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        if not store.delete_run(s, sc.user.id, run_id):
            raise not_found()
    return {"ok": True}


class StepIn(BaseModel):
    """One saved manual step. Accepts both the ManualStep shape and the exported transition shape ('reward')."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    parent_smiles: str = Field(max_length=2000)
    child_smiles: str = Field(max_length=2000)
    edit_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_:.-]+$")
    site: list[int] = Field(default_factory=list, max_length=64)
    reward_delta: float | None = Field(
        default=None,
        ge=-1e6,
        le=1e6,
        allow_inf_nan=False,
        validation_alias=AliasChoices("reward_delta", "reward"),
    )
    t: float = Field(default=0.0, ge=0.0, le=1e9, allow_inf_nan=False)
    seed_smiles: str | None = Field(default=None, max_length=2000)


class ManualIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed_smiles: str | None = Field(default=None, max_length=2000)
    target_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    steps: list[StepIn] | None = Field(default=None, max_length=MAX_MANUAL_STEPS)
    jsonl: str | None = Field(default=None, max_length=1_000_000)  # the manual export file, verbatim


def _manual_summary(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": m["id"],
        "seed_smiles": m["seed_smiles"],
        "target_id": m["target_id"],
        "created_at": m["created_at"],
        "n_steps": m.get("n_steps", 0),
    }


@library.get("/manual")
def list_manual(sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        rows = store.list_manual(s, sc.user.id)
    return {"sessions": [_manual_summary(m) for m in rows]}


@library.post("/manual", status_code=201)
def save_manual(body: ManualIn, sc: Scope = Depends(scope)) -> dict:
    throttle(sc, "lib_write", 30)
    steps = body.steps
    if steps is None and body.jsonl is not None:
        steps = []
        for line in body.jsonl.splitlines():
            if not line.strip():
                continue
            if len(steps) >= MAX_MANUAL_STEPS:
                raise HTTPException(422, f"steps must contain 1..{MAX_MANUAL_STEPS} items")
            try:
                steps.append(StepIn.model_validate_json(line))
            except ValidationError:
                raise HTTPException(422, "jsonl contains a line that is not a valid manual step") from None
    if not steps or len(steps) > MAX_MANUAL_STEPS:
        raise HTTPException(422, f"steps must contain 1..{MAX_MANUAL_STEPS} items")
    seed_raw = body.seed_smiles or steps[0].seed_smiles or steps[0].parent_smiles
    try:
        seed = molio.canonical(molio.parse_molecule(seed_raw))
        for st_ in steps:
            molio.parse_molecule(st_.parent_smiles)
            molio.parse_molecule(st_.child_smiles)
    except molio.InvalidMolecule as exc:
        raise HTTPException(422, str(exc)) from None
    if any(i < 0 or i > 10_000 for st_ in steps for i in st_.site):
        raise HTTPException(422, "site index out of range")
    rows = [
        {
            "parent_smiles": x.parent_smiles,
            "child_smiles": x.child_smiles,
            "edit_id": x.edit_id,
            "site": x.site,
            "reward_delta": x.reward_delta,
            "t": x.t,
        }
        for x in steps
    ]
    with sc.session() as s:
        store.lock_owner(s, sc.user.id, "manual")
        _cap(store.count_manual(s, sc.user.id), store.MAX_MANUAL, "manual sessions")
        row = store.insert_manual(s, sc.user.id, seed, body.target_id, rows)
    return _manual_summary(row)


@library.get("/manual/{sid}")
def get_manual(sid: uuid.UUID, sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        m = store.get_manual(s, sc.user.id, sid)
    if m is None:
        raise not_found()
    return {**_manual_summary(m), "steps": m["steps"]}


@library.delete("/manual/{sid}")
def delete_manual(sid: uuid.UUID, sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        if not store.delete_manual(s, sc.user.id, sid):
            raise not_found()
    return {"ok": True}


# ================================================================================================ profiles
def _profile_out(p: dict[str, Any]) -> dict[str, Any]:
    return {"id": p["id"], "created_at": p["created_at"], **p["body"]}


def _parse_profile(raw: dict[str, Any]) -> ProfileBody:
    try:
        return ProfileBody.model_validate(raw)
    except ValidationError as exc:
        e = exc.errors()[0]
        loc = ".".join(str(x) for x in e["loc"])
        msg = str(e["msg"]).removeprefix("Value error, ")
        raise HTTPException(422, f"{loc}: {msg}" if loc else msg) from None


class SmartsCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patterns: list[str] = Field(max_length=MAX_SMARTS)


@profiles.post("/check-smarts")
def check_smarts_route(
    body: SmartsCheck, sc: Scope = Depends(scope), st: LeadState = Depends(get_state)
) -> dict:
    """Live validation for the editor: [{ok, error}] per pattern. Stores nothing. At most MAX_SMARTS patterns of
    MAX_SMARTS_LEN characters each, parsed under the process-wide heavy-operation gate (busy -> 429)."""
    throttle(sc, "smarts", 120)
    with st.heavy:
        return {"results": [{"ok": (e := check_smarts(p)) is None, "error": e} for p in body.patterns]}


@profiles.get("")
def list_profiles(sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        rows = store.list_profiles(s, sc.user.id)
    return {"profiles": [_profile_out(p) for p in rows]}


@profiles.post("", status_code=201)
def create_profile(raw: dict[str, Any], sc: Scope = Depends(scope)) -> dict:
    throttle(sc, "lib_write", 30)
    body = _parse_profile(raw)
    with sc.session() as s:
        store.lock_owner(s, sc.user.id, "profile")
        _cap(store.count_profiles(s, sc.user.id), store.MAX_PROFILES, "profiles")
        row = store.insert_profile(s, sc.user.id, body.name, body.model_dump(mode="json"))
    return _profile_out(row)


def _owned(s: Session, uid: uuid.UUID, pid: uuid.UUID) -> dict[str, Any]:
    row = store.get_profile(s, uid, pid)
    if row is None:
        raise not_found()
    return row


@profiles.get("/{pid}")
def get_profile(pid: uuid.UUID, sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        return _profile_out(_owned(s, sc.user.id, pid))


@profiles.put("/{pid}")
def update_profile(pid: uuid.UUID, raw: dict[str, Any], sc: Scope = Depends(scope)) -> dict:
    throttle(sc, "lib_write", 30)
    body = _parse_profile(raw)
    with sc.session() as s:
        row = store.update_profile(s, sc.user.id, pid, body.name, body.model_dump(mode="json"))
        if row is None:
            raise not_found()
    return _profile_out(row)


@profiles.delete("/{pid}")
def delete_profile(pid: uuid.UUID, sc: Scope = Depends(scope)) -> dict:
    with sc.session() as s:
        if not store.delete_profile(s, sc.user.id, pid):
            raise not_found()
    return {"ok": True}


router.include_router(api)
router.include_router(library)
router.include_router(profiles)
