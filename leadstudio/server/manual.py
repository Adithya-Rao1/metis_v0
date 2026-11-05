"""Manual mode: preview one hand-picked edit, and keep a per-user live session of accepted steps.

Live sessions are held in memory, keyed by the owning Metis user: a session id of another user is "unknown". Saving
a session to the library (database, `lead_manual_session` / `lead_manual_step`) is a separate, explicit route.
Nothing is written to disk and no SMILES are logged. Transitions are exported in the policy format
{parent_smiles, edit_id, site, child_smiles, reward} with chosen_by="user"; `reward` is the change in the RDKit
composite, not binding.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any

from fastapi import HTTPException
from rdkit import Chem

from leadstudio.schema import (
    EditRecord,
    ManualPreview,
    ManualPreviewRequest,
    ManualSession,
    ManualStep,
    RunConfig,
)
from leadstudio.server import molio
from leadstudio.server.core import Rejected

log = logging.getLogger("leadstudio.server")

MAX_APPLICABLE = 500
MAX_LOCKED = 60


def transition(sess: ManualSession, st: ManualStep) -> dict:
    return {
        "parent_smiles": st.parent_smiles,
        "edit_id": st.edit_id,
        "site": list(st.site),
        "child_smiles": st.child_smiles,
        "reward": st.reward_delta,
        "chosen_by": "user",
        "t": round(st.t, 3),
        "session_id": sess.session_id,
        "seed_smiles": sess.seed_smiles,
        "target_id": sess.target_id,
    }


def export_jsonl(sess: ManualSession) -> str:
    return "".join(json.dumps(transition(sess, st)) + "\n" for st in sess.steps)


class Sessions:
    """In-memory live sessions, per owner, bounded per user and overall."""

    MAX_TOTAL = 2000

    def __init__(self, per_user: int, max_steps: int):
        self.per_user, self.max_steps = per_user, max_steps
        self.items: OrderedDict[str, tuple[uuid.UUID, ManualSession]] = OrderedDict()
        self.started: dict[str, float] = {}
        self.lock = threading.Lock()

    def create(self, owner: uuid.UUID, seed: str, target_id: str | None) -> ManualSession:
        with self.lock:
            mine = [k for k, (o, _) in self.items.items() if o == owner]
            while len(mine) >= self.per_user:  # drop this user's oldest, never anyone else's
                old = mine.pop(0)
                self.items.pop(old, None)
                self.started.pop(old, None)
            s = ManualSession(session_id=secrets.token_urlsafe(9), seed_smiles=seed, target_id=target_id)
            self.items[s.session_id] = (owner, s)
            self.started[s.session_id] = time.monotonic()
            while len(self.items) > self.MAX_TOTAL:
                k, _ = self.items.popitem(last=False)
                self.started.pop(k, None)
            return s.model_copy(deep=True)

    def get(self, sid: str, owner: uuid.UUID) -> ManualSession | None:
        with self.lock:
            hit = self.items.get(sid)
            if hit is None or hit[0] != owner:
                return None
            return hit[1].model_copy(deep=True)

    def append(self, sid: str, owner: uuid.UUID, st: ManualStep) -> ManualSession | None:
        with self.lock:
            hit = self.items.get(sid)
            if hit is None or hit[0] != owner:
                return None
            s = hit[1]
            if len(s.steps) >= self.max_steps:
                raise Rejected(429, f"Session is limited to {self.max_steps} steps")
            s.steps.append(st.model_copy(update={"t": time.monotonic() - self.started[sid]}))
            return s.model_copy(deep=True)


class ManualService:
    def __init__(self, svc: Any, scorer: Any, settings: Any):
        self.svc, self._scorer, self.s = svc, scorer, settings
        self.sessions = Sessions(settings.max_live_sessions_per_user, settings.max_live_steps)
        self._lock = threading.Lock()

    def rdkit_scorer(self):
        with self._lock:
            if self._scorer is None:
                from leadstudio.scoring import RDKitScorer

                self._scorer = RDKitScorer()
            return self._scorer

    def registry(self):
        try:
            return self.svc.registry()
        except Exception as exc:
            raise HTTPException(503, f"edit registry unavailable ({type(exc).__name__})") from None

    @staticmethod
    def sanity(parent: str, child: str):
        try:
            from leadstudio.sanity import check_child
        except Exception:
            return True, None  # no sanity module on this build
        ok, why = check_child(parent, child)
        return bool(ok), None if ok else str(why)

    @staticmethod
    def check_smiles(smi: str) -> None:
        try:
            molio.parse_molecule(smi)
        except molio.InvalidMolecule as exc:
            raise HTTPException(422, str(exc)) from None

    def applicable(self, smiles: str, locked: str) -> list[dict]:
        self.check_smiles(smiles)
        try:
            lock = [int(x) for x in locked.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(422, "locked must be comma-separated atom indices") from None
        if len(lock) > MAX_LOCKED:
            raise HTTPException(422, "too many locked atoms")
        out: list[dict] = []
        for op, site in self.registry().applicable(smiles, lock):
            out.append(
                {
                    "edit_id": op.id,
                    "name": op.name,
                    "category": op.category,
                    "site": list(site),
                    "rationale": op.rationale,
                }
            )
            if len(out) >= MAX_APPLICABLE:
                break
        return out

    def preview(self, req: ManualPreviewRequest) -> ManualPreview:
        self.check_smiles(req.smiles)
        if req.target_id is not None and self.svc.get_target(req.target_id) is None:
            raise HTTPException(422, "unknown target_id")
        reg = self.registry()
        try:
            op = reg.get(req.edit_id)
        except KeyError:
            return ManualPreview(ok=False, reason="unknown edit")
        n = Chem.MolFromSmiles(req.smiles).GetNumAtoms()
        if not req.site or any(i < 0 or i >= n for i in req.site):
            return ManualPreview(ok=False, reason="site index out of range")
        products = op.apply(req.smiles, list(req.site))
        if not products:
            return ManualPreview(ok=False, reason="edit does not apply at this site")
        child = products[0]
        sane, why = self.sanity(req.smiles, child)

        cfg = RunConfig(seed_smiles=req.smiles)
        sc = self.rdkit_scorer()
        p_score, c_score = sc.score(req.smiles, cfg), sc.score(child, cfg)
        if req.target_id is not None:
            bs = self.svc.binding_scorer()
            if bs is not None:
                try:
                    bcfg = RunConfig(seed_smiles=req.smiles, stage="binding", target_id=req.target_id)
                    c_score = c_score.model_copy(
                        update={"extras": {**c_score.extras, **bs.score(child, bcfg).extras}}
                    )
                    p_score = p_score.model_copy(
                        update={"extras": {**p_score.extras, **bs.score(req.smiles, bcfg).extras}}
                    )
                except Exception as exc:  # binding is optional decoration on the preview
                    log.warning("binding extras failed: %s", type(exc).__name__)
        rec = EditRecord(
            edit_id=op.id,
            name=op.name,
            category=op.category,
            site=list(req.site),
            chosen_by="user",
            parents=[req.smiles],
        )
        expl = self.svc.explainer().explain(req.smiles, child, rec, p_score, c_score, op)

        from leadstudio.engine import geometry

        try:
            _, pmol = geometry.embed_3d(req.smiles)
            block, cmol = geometry.embed_3d(child, pmol)
            amap = geometry.atom_map(pmol, cmol)
        except ValueError:
            return ManualPreview(
                ok=False,
                child_smiles=child,
                score=c_score,
                explanation=expl,
                sane=sane,
                reason="could not build a 3D structure for the product",
            )
        return ManualPreview(
            ok=True,
            child_smiles=child,
            molblock=block,
            atom_map=amap,
            score=c_score,
            explanation=expl,
            sane=sane,
            reason=why,
        )

    def create_session(self, owner: uuid.UUID, seed_smiles: str, target_id: str | None) -> ManualSession:
        try:
            seed = molio.canonical(molio.parse_molecule(seed_smiles))
        except molio.InvalidMolecule as exc:
            raise HTTPException(422, str(exc)) from None
        if target_id is not None and self.svc.get_target(target_id) is None:
            raise HTTPException(422, "unknown target_id")
        return self.sessions.create(owner, seed, target_id)

    def add_step(self, owner: uuid.UUID, session_id: str, step: ManualStep) -> ManualSession:
        if self.sessions.get(session_id, owner) is None:
            raise HTTPException(404, "unknown session")
        self.check_smiles(step.parent_smiles)
        self.check_smiles(step.child_smiles)
        if step.reward_delta is None:  # server-side RDKit composite delta
            sc, cfg = self.rdkit_scorer(), RunConfig(seed_smiles=step.parent_smiles)
            step = step.model_copy(
                update={
                    "reward_delta": sc.score(step.child_smiles, cfg).composite
                    - sc.score(step.parent_smiles, cfg).composite
                }
            )
        s = self.sessions.append(session_id, owner, step)
        if s is None:
            raise HTTPException(404, "unknown session")
        return s
