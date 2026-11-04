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
USER = {"id": "bbbbbbbb-0000-4000-8000-000000000001", "tier": "free", "email_verified": True, "terms_current": True}
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 80" width="120" height="80"><rect width="120" height="80" fill="#fff"/><path d="M20 40 L50 20 L80 40 L50 60 Z" fill="none" stroke="#333"/></svg>'
SMILES = ["CCO", "c1ccccc1O", "CC(=O)Nc1ccc(O)cc1", "CC(C)Cc1ccc(cc1)C(C)C(=O)O"]
CARDS = {
    "logd": dict(kind="regression", units="log units", default_range=(-2.0, 6.0), mw={"3": 1.2, "5": 1.0, "10": 0.9, "20": 0.8}),
    "ames": dict(kind="classification", units=None, default_range=(0.0, 1.0), mw={}),
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
                default_range=list(c["default_range"]), median_calibration_width_by_n=c["mw"], weights_sha256="0" * 64)


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
    def __init__(self, models=True, projects=True, predicted=True, signed_in=True, tier="free", ranked=None):
        self.models, self.projects, self.predicted, self.signed_in = models, projects, predicted, signed_in
        self.user = {**USER, "tier": tier}
        self.config = ranked
        self.calls: list[tuple[str, str, object]] = []
        self.overrides: dict[tuple[str, str], tuple[int, object, dict]] = {}   # (method, path regex) -> (status, body, headers)
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
            out.append({"id": cid, "smiles": s, "canonical_smiles": s, "label": f"cmpd {i + 1}", "created_at": "2026-01-01T00:00:00Z", "predictions": preds, "measurements": []})
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
            return 200, {"projects": ([{"id": PID, "name": "Series A (mock)", "created_at": "2026-01-01T00:00:00Z", "compound_count": 4}] if self.projects else [])}, {}
        if path == f"/projects/{PID}" and method == "GET":
            return 200, {"id": PID, "name": "Series A (mock)", "created_at": "2026-01-01T00:00:00Z", "max_compounds": 10, "compounds": self.compounds() if self.projects else []}, {}
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
