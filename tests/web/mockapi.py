"""A small in-test stand-in for the API, installed with Playwright request interception (`page.route`).

tests/web runs against a static server only, so these tests need an API to talk to. The data below is LAYOUT DATA FOR
TESTS, built from the real contract (backend/schemas/contracts.py) and the real ranking engine; it is never shipped and
never claimed as a model result. The real API is exercised in tests/e2e.
"""
from __future__ import annotations

import json
import re
import uuid

from rdkit import Chem
from rdkit.Chem import AllChem

from backend.ranking.engine import EndpointInfo, rank_compounds
from backend.schemas.contracts import Prediction, RankingConfig, UncertaintyBreakdown

MV = uuid.UUID("11111111-1111-1111-1111-111111111111")
PID = "aaaaaaaa-0000-4000-8000-000000000001"
MEAS_ID = "ffffffff-0000-4000-8000-0000000000a1"
USER = {"id": "bbbbbbbb-0000-4000-8000-000000000001", "email": "analyst@example.org", "tier": "free", "email_verified": True, "terms_current": True}
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 80" width="120" height="80"><rect width="120" height="80" fill="#fff"/><path d="M20 40 L50 20 L80 40 L50 60 Z" fill="none" stroke="#333"/></svg>'
SMILES = ["CCO", "c1ccccc1O", "CC(=O)Nc1ccc(O)cc1", "CC(C)Cc1ccc(cc1)C(C)C(=O)O"]
CARDS = {
    "logd": dict(kind="regression", units="log units", default_range=(-2.0, 6.0), mw={"3": 1.2, "5": 1.0, "10": 0.9, "20": 0.8}, hib=False),
    "ames": dict(kind="classification", units=None, default_range=(0.0, 1.0), mw={}, hib=None),
}


def molblock(smi: str) -> str:
    m = Chem.AddHs(Chem.MolFromSmiles(smi))
    AllChem.EmbedMolecule(m, randomSeed=7)
    return Chem.MolToMolBlock(m)


def card(ep: str) -> dict:
    c = CARDS[ep]
    return dict(endpoint=ep, version="mock-1", kind=c["kind"], units=c["units"],
                datasets=[{"name": "Mock dataset", "url": "https://example.org/mock", "license": "CC0 (mock)", "size": 1000, "split": "scaffold"}],
                limitations="Mock model card used by browser layout tests only.", split_type="scaffold", benchmark={"rmse": {"model": 0.9, "baseline": 1.1}, "n_test": 120},
                coverage_by_n={"3": 0.88, "5": 0.9, "10": 0.9, "20": 0.91}, runtime_by_n_seconds={"3": 0.5, "5": 1.0, "10": 2.0, "20": 4.0},
                default_range=list(c["default_range"]), median_calibration_width_by_n=c["mw"], weights_sha256="0" * 64,
                **({"higher_is_better": c["hib"]} if c.get("hib") is not None else {}))


def prediction(ep: str, i: int, n: int = 5) -> dict:
    bd = UncertaintyBreakdown(ensemble_disagreement=0.5, similarity_to_training=0.3, assay_noise=0.2)
    if CARDS[ep]["kind"] == "regression":
        v = 0.5 + i * 1.1
        p = Prediction(endpoint=ep, kind="regression", model_version_id=MV, ensemble_n=n, value=v, interval_low=v - 0.6, interval_high=v + 0.6,
                       coverage=0.9, sample_size=1200, method_note="mock", breakdown=bd)
    else:
        pr = 0.15 + i * 0.22
        p = Prediction(endpoint=ep, kind="classification", model_version_id=MV, ensemble_n=n, value=pr, probability=pr, coverage=0.9, sample_size=900,
                       method_note="mock", breakdown=bd)
    return p.model_dump(mode="json")


class MockApi:
    def __init__(self, models=True, projects=True, predicted=True, signed_in=True, tier="free", ranked=None, measured=False, depiction_route=True):
        self.models, self.projects, self.predicted, self.signed_in = models, projects, predicted, signed_in
        self.project_name = "Series A (mock)"
        self.deleted_compounds: set[str] = set()
        self.deleted_measurements: set[str] = set()
        self.measured = measured                     # first compound carries one measurement (id ...0aa1)
        self.depiction_route = depiction_route       # POST /dockprep/depiction answers an SVG; False: the route does not exist (404)
        self.user = {**USER, "tier": tier}
        self.config = ranked
        self.calls: list[tuple[str, str, object]] = []
        self.overrides: dict[tuple[str, str], tuple[int, object, dict]] = {}   # (method, path regex) -> (status, body, headers)
        self.notes: list[dict] = []                  # project notes (stateful: POST/PATCH/DELETE change this list)
        self.consents = {"train_models": False, "share_dataset": False, "analytics": False, "contact": False}

    # ---- data ----
    def compounds(self):
        out = []
        for i, s in enumerate(SMILES):
            cid = f"cccccccc-0000-4000-8000-00000000000{i + 1}"
            preds = []
            if self.predicted:
                for ep in CARDS:
                    preds.append({"id": f"dddddddd-0000-4000-8000-0000000000{i}{list(CARDS).index(ep)}", "label": "predicted", "prediction": prediction(ep, i)})
            ms = [{"id": MEAS_ID, "endpoint": "logd", "value": 1.5, "unit": "log units"}] if self.measured and i == 0 and MEAS_ID not in self.deleted_measurements else []
            if cid in self.deleted_compounds:
                continue
            out.append({"id": cid, "smiles": s, "canonical_smiles": s, "label": f"cmpd {i + 1}", "created_at": "2026-01-01T00:00:00Z", "predictions": preds, "measurements": ms})
        return out

    def rank(self, cfg: dict):
        config = RankingConfig(**cfg)
        comps = self.compounds()
        preds = {c["id"]: {p["prediction"]["endpoint"]: Prediction(**p["prediction"]) for p in c["predictions"]} for c in comps}
        info = {ep: EndpointInfo(default_range=tuple(CARDS[ep]["default_range"]), median_width_by_n={int(k): v for k, v in CARDS[ep]["mw"].items()}) for ep in CARDS}
        ranked = rank_compounds(preds, config, info)
        return [{**r.model_dump(mode="json"), "stale_endpoints": []} for r in ranked]

    # ---- routing ----
    def handler(self, route):
        req = route.request
        path = re.sub(r"^.*?/api", "", req.url.split("?")[0])
        method = req.method
        body = None
        try:
            body = req.post_data_json
        except Exception:
            body = None
        self.calls.append((method, path, body))
        for (m, rx), (status, payload, headers) in self.overrides.items():
            if m == method and re.search(rx, path):
                return self._json(route, status, payload, headers)
        status, payload, headers = self.dispatch(method, path, body)
        if isinstance(payload, (bytes, str)) and headers.get("content-type", "").startswith(("image/", "text/", "application/octet")):
            return route.fulfill(status=status, body=payload, headers=headers)
        return self._json(route, status, payload, headers)

    @staticmethod
    def _json(route, status, payload, headers=None):
        h = {"content-type": "application/json", **(headers or {})}
        route.fulfill(status=status, body=json.dumps(payload) if payload is not None else "", headers=h)

    def dispatch(self, method, path, body):
        if path == "/auth/csrf":
            return 200, {"csrf_token": "x"}, {"set-cookie": "metis_csrf=testtoken; Path=/"}
        if path == "/auth/terms":
            return 200, {"version": "2026-01", "documents": ["terms", "privacy", "disclaimer"]}, {}
        if path == "/auth/me":
            return (200, self.user, {}) if self.signed_in else (401, {"detail": "not_authenticated"}, {})
        if not self.signed_in and path not in ("/models",) and not path.startswith(("/models", "/auth")):
            return 401, {"detail": "not_authenticated"}, {}
        if path == "/auth/login":
            return 200, {"user": self.user, "csrf_token": "x"}, {}
        if path == "/auth/register":
            return 202, {"status": "verification_sent"}, {}
        if path == "/models":
            return 200, {"models": [card(e) for e in CARDS] if self.models else []}, {}
        if m := re.fullmatch(r"/models/([a-z0-9_]+)", path):
            return (200, card(m.group(1)), {}) if m.group(1) in CARDS else (404, {"detail": "not found"}, {})
        if path == "/projects" and method == "GET":
            return 200, {"projects": ([{"id": PID, "name": self.project_name, "created_at": "2026-01-01T00:00:00Z", "compound_count": 4}] if self.projects else [])}, {}
        if path == f"/projects/{PID}" and method == "GET":
            return 200, {"id": PID, "name": self.project_name, "created_at": "2026-01-01T00:00:00Z", "max_compounds": 10, "compounds": self.compounds() if self.projects else []}, {}
        if path == f"/projects/{PID}" and method == "PATCH":
            name = (body or {}).get("name")
            if not isinstance(name, str) or not name.strip():
                return 422, {"detail": "name required"}, {}
            self.project_name = name.strip()
            return 200, {"id": PID, "name": self.project_name, "created_at": "2026-01-01T00:00:00Z"}, {}
        if path == f"/projects/{PID}/notes" and method == "GET":
            return 200, {"notes": self.notes, "max_notes": 200, "max_chars": 5000}, {}
        if path == f"/projects/{PID}/notes" and method == "POST":
            b = (body or {}).get("body")
            if not isinstance(b, str) or not b.strip() or len(b) > 5000:
                return 422, {"detail": "invalid note"}, {}
            n = {"id": f"cccccccc-0000-4000-8000-{len(self.notes) + 1:012d}", "body": b.strip(), "created_at": "2026-01-02T00:00:00Z"}
            self.notes.append(n)
            return 201, n, {}
        if m := re.fullmatch(rf"/projects/{PID}/notes/([^/]+)", path):
            hit = next((n for n in self.notes if n["id"] == m.group(1)), None)
            if hit is None:
                return 404, {"detail": "not found"}, {}
            if method == "PATCH":
                b = (body or {}).get("body")
                if not isinstance(b, str) or not b.strip():
                    return 422, {"detail": "invalid note"}, {}
                hit["body"] = b.strip()
                return 200, hit, {}
            if method == "DELETE":
                self.notes.remove(hit)
                return 204, None, {}
        if m := re.fullmatch(rf"/projects/{PID}/compounds/([^/]+)", path):
            if method == "DELETE":
                self.deleted_compounds.add(m.group(1))
                return 204, None, {}
        if m := re.fullmatch(rf"/projects/{PID}/measurements/([^/]+)", path):
            if method == "DELETE":
                self.deleted_measurements.add(m.group(1))
                return 204, None, {}
        if path == "/dockprep/depiction" and method == "POST":
            return (200, SVG, {"content-type": "image/svg+xml"}) if self.depiction_route else (404, {"detail": "not found"}, {})
        if path == f"/projects/{PID}/ranking" and method == "GET":
            return 200, {"config": self.config, "persisted": self.config is not None}, {}
        if path == f"/projects/{PID}/ranking" and method == "PUT":
            self.config = body
            try:
                ranking = self.rank(body)
            except Exception as e:  # noqa: BLE001 - mirrors the API's 422
                return 422, {"detail": str(e)}, {}
            return 200, {"config": body, "persisted": True, "formula": "R_j = (1 - lam) S_j + lam C_j", "stale_model_version": False, "stale_predictions": [], "ranking": ranking}, {}
        if m := re.fullmatch(rf"/projects/{PID}/compounds/([^/]+)/depiction\.svg", path):
            return 200, SVG, {"content-type": "image/svg+xml"}
        if m := re.fullmatch(rf"/projects/{PID}/compounds/([^/]+)/conformer", path):
            idx = int(m.group(1)[-1]) - 1
            return 200, {"compound_id": m.group(1), "molblock": molblock(SMILES[idx]), "method": "ETKDGv3 + MMFF94 (mock)", "label": "computed", "note": "computed", "persisted": False}, {}
        if path == f"/projects/{PID}/compounds" and method == "POST":
            return 201, {"compounds": [{"id": "x", "smiles": s, "canonical_smiles": s, "label": None} for s in (body.get("smiles") or ["C"])]}, {}
        if path == f"/projects/{PID}/predict":
            return 202, {"job_id": "eeeeeeee-0000-4000-8000-000000000001", "state": "queued", "total": 8, "estimated_seconds": 3.0, "ensemble_n": body["ensemble_n"], "label": "predicted"}, {}
        if path.startswith("/jobs/"):
            return 200, {"job_id": path[6:], "project_id": PID, "state": "done", "done": 8, "total": 8, "estimated_seconds": 3.0, "error": None, "prediction_ids": []}, {}
        if m := re.fullmatch(r"/predictions/([^/]+)/uncertainty", path):
            ep = list(CARDS)[int(m.group(1)[-1])]
            i = int(m.group(1)[-2])
            return 200, {"prediction": prediction(ep, i), "fragments": [{"atom_indices": [0, 1], "share": 0.6, "share_low": 0.5, "share_high": 0.7, "train_frequency": 0.1}],
                         "nearest_training": [{"smiles": "CC", "similarity": 0.5, "label": 1.0}], "runtime_seconds": 0.12}, {}
        if path == f"/projects/{PID}/export":
            return 200, "# disclaimer\n# line two\n# line three\ncompound,label\n1,a\n", {"content-type": "text/csv", "content-disposition": 'attachment; filename="metis_export.csv"'}
        if path == "/consents":
            if method == "POST":
                self.consents[body["scope"]] = body["granted"]
            return 200, {"consents": {k: {"granted": v, "granted_at": None} for k, v in self.consents.items()}, "history": []}, {}
        if path.startswith("/dockprep/"):
            return 503, {"detail": {"code": "fetch_disabled", "message": "Fetching by PDB ID is not enabled on this server; upload the file instead."}}, {}
        return 404, {"detail": "not found"}, {}


def install(page, api: MockApi):
    page.route("**/api/**", api.handler)
    page.api = api


# ======================================================================================================================
# Lead Studio (web/lead/): a stand-in for the routes the backend agent implements under /lead (the contract in
# docs/agent_prompts/96_lead_studio_web.md). Shapes are those of studio/schema.py; run events come from a trimmed real recording
# (tests/web/lead_fixtures/binding_run.json). LAYOUT/BEHAVIOUR DATA FOR TESTS: never shipped, never claimed as a model result.
# ======================================================================================================================
LEAD_FX = __import__("pathlib").Path(__file__).parent / "lead_fixtures"
LEAD_PATH = re.compile(r".*/lead/(api|library|profiles)(/|\?|$).*")
HOSTILE = '<img src=x onerror="window.__pwn=1"><b id="pwn">'
_TEXT_KEYS = {"name", "rationale", "headline", "family", "note", "category", "edit_id", "reason", "error", "message", "title"}
_SMILES_KEYS = {"smiles", "parent_smiles", "seed_smiles", "final_smiles", "canonical_smiles"}


def _hostile(obj, key=None):
    """Recursively replace display strings with markup (SMILES keep their text and get a markup suffix, consistently, so lookups still match)."""
    if isinstance(obj, dict):
        return {k: _hostile(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_hostile(v, key) for v in obj]
    if isinstance(obj, str):
        if key in _SMILES_KEYS:
            return obj + HOSTILE
        if key in _TEXT_KEYS or key == "tags":
            return HOSTILE + obj
    return obj


class LeadMockApi:
    """Install on a page AFTER the Metis MockApi (later routes win): `install_lead(page, lead)`. Session comes from the Metis MockApi (/api/auth/me)."""

    def __init__(self, hostile=False, fail_health=False, events_delay_first=False, pose_polls=0):
        self.hostile, self.fail_health = hostile, fail_health
        self.pose_polls, self._pose_seen = pose_polls, 0     # a pose job reports "running" this many polls before it is done
        self.calls: list[tuple[str, str, object]] = []
        self.runs: dict[str, dict] = {}
        self.status_overrides: dict[tuple[str, str], tuple[int, object]] = {}   # (method, path regex) -> (status, body)
        self.targets = json.loads((LEAD_FX / "targets.json").read_text())
        self.events = json.loads((LEAD_FX / "binding_run.json").read_text())
        self.library: list[dict] = []
        self.profiles: list[dict] = []
        if hostile:
            self.targets = _hostile(self.targets)
            self.events = _hostile(self.events)

    # ---- helpers ----
    @staticmethod
    def _sse(events) -> str:
        return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)

    def validate(self, body):
        smi = ((body or {}).get("smiles") or "").replace(HOSTILE, "") or None   # the client sends back what it was given
        mol = Chem.MolFromSmiles(smi) if smi else None
        if mol is None:
            err = f"Could not parse {smi!r}" if smi else "empty"
            return 200, {"ok": False, "error": (HOSTILE + err) if self.hostile else err}
        canon = Chem.MolToSmiles(mol)
        seed = self.events[0]["payload"]
        return 200, {"ok": True, "canonical_smiles": canon + (HOSTILE if self.hostile else ""), "heavy_atoms": mol.GetNumHeavyAtoms(),
                     "molblock": molblock(canon), "score": seed["seed_score"], "error": None}

    # ---- routing ----
    def handler(self, route):
        req = route.request
        path = re.sub(r"^.*?/lead", "", req.url.split("?")[0])
        method = req.method
        try:
            body = req.post_data_json
        except Exception:
            body = None
        self.calls.append((method, path, body))
        for (m, rx), (status, payload) in self.status_overrides.items():
            if m == method and re.search(rx, path):
                return route.fulfill(status=status, body=json.dumps(payload), headers={"content-type": "application/json"})
        status, payload, headers = self.dispatch(method, path, body)
        if isinstance(payload, (str, bytes)):
            return route.fulfill(status=status, body=payload, headers=headers)
        return route.fulfill(status=status, body=json.dumps(payload) if payload is not None else "", headers={"content-type": "application/json", **headers})

    def dispatch(self, method, path, body):
        if path == "/api/health":
            return (200, {"ok": True, "stub": False}, {}) if not self.fail_health else (503, {"detail": "down"}, {})
        if path == "/api/targets":
            return 200, self.targets, {}
        if m := re.fullmatch(r"/api/targets/([\w-]+)", path):
            hit = [t for t in self.targets if t["id"] == m.group(1)]
            return (200, hit[0], {}) if hit else (404, {"detail": "not found"}, {})
        if path.startswith("/api/data/"):
            return 200, (LEAD_FX / "fixture1.pdb").read_text(), {"content-type": "chemical/x-pdb"}
        if path == "/api/poses" and method == "POST":
            self._pose_seen = 0
            return 200, {"job_id": "pose-1", "status": "queued"}, {}
        if re.fullmatch(r"/api/poses/[\w-]+", path):
            self._pose_seen += 1
            if self._pose_seen <= self.pose_polls:
                return 200, {"job_id": "pose-1", "status": "running", "progress": 0.3}, {}
            smi = "CCO"
            return 200, {"job_id": "pose-1", "status": "done", "progress": 1, "pose_confidence": 0.66, "affinity": -7.1, "note": "mock pose (test data)",
                         "ligand_sdf": molblock(smi), "protein_pdb": (LEAD_FX / "fixture1.pdb").read_text(), "apo_pdb": (LEAD_FX / "fixture1.pdb").read_text()}, {}
        if path == "/api/presets":
            return 200, [], {}
        if path == "/api/molecules/validate":
            return self.validate(body) + ({},)
        if path == "/api/runs" and method == "POST":
            rid = f"lead-run-{len(self.runs) + 1}"
            self.runs[rid] = body
            return 200, {"run_id": rid}, {}
        if m := re.fullmatch(r"/api/runs/([\w-]+)/events", path):
            evs = [{**e, "run_id": m.group(1)} for e in self.events]
            return 200, self._sse(evs), {"content-type": "text/event-stream", "cache-control": "no-cache"}
        if m := re.fullmatch(r"/api/runs/([\w-]+)", path):
            if method == "DELETE":
                return 204, None, {}
            return 200, {"run_id": m.group(1), "status": "done"}, {}
        if path == "/api/manual/applicable":
            name = (HOSTILE if self.hostile else "") + "Swap C for N"
            return 200, {"edits": [{"edit_id": "atom_swap_c_n", "name": name, "category": "atom", "site": [0], "rationale": name, "tags": ["polarity"]}]}, {}
        if path == "/api/manual/sessions" and method == "POST":
            return 200, {"session_id": "sess-1", "seed_smiles": (body or {}).get("seed_smiles", ""), "target_id": (body or {}).get("target_id"), "steps": []}, {}
        if path == "/library/runs":
            if method == "POST":
                self.library.append({"id": f"lib-{len(self.library) + 1}", "title": (body or {}).get("title") or "run", "stage": "binding", "created_at": "2026-01-01T00:00:00Z"})
                return 201, {"id": self.library[-1]["id"]}, {}
            return 200, {"runs": self.library}, {}
        if path == "/library/manual":
            return 200, {"sessions": []}, {}
        if path == "/profiles":
            return 200, {"profiles": self.profiles}, {}
        return 404, {"detail": "not found"}, {}


def install_lead(page, lead: LeadMockApi):
    """Route /lead/{api,library,profiles}/** to `lead`. Call after `install` (Metis MockApi) so these routes take precedence over `**/api/**`."""
    page.route(LEAD_PATH, lead.handler)
    page.lead = lead
