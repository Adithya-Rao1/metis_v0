import uuid

import pytest

from backend.exports.formats import disclaimer
from backend.schemas.contracts import Prediction
from backend.schemas.interfaces import PredictionDetail
from api_support import H


def run_job(client, queue, pid, who="a", **body):
    body.setdefault("endpoints", ["logd", "ames"])
    r = client.post(f"/projects/{pid}/predict", json=body, headers=H(who))
    assert r.status_code == 202, r.text
    jid = r.json()["job_id"]
    queue.wait(uuid.UUID(jid))
    return r.json(), client.get(f"/jobs/{jid}", headers=H(who)).json()


def test_predict_job_persists_valid_predictions(client, queue, loaded):
    pid, comps = loaded
    start, job = run_job(client, queue, pid, ensemble_n=5)
    assert start["estimated_seconds"] == pytest.approx(2 * 0.5 * 5 * 3)
    assert job["state"] == "done" and job["done"] == job["total"] == 6 and len(job["prediction_ids"]) == 6
    d = client.get(f"/projects/{pid}", headers=H("a")).json()
    for c in d["compounds"]:
        assert {p["prediction"]["endpoint"] for p in c["predictions"]} == {"logd", "ames"}
        for p in c["predictions"]:
            Prediction.model_validate(p["prediction"])  # every prediction response validates against the contract
            assert p["label"] == "predicted"
            assert not {"verdict", "status", "pass", "fail"} & (set(p) | set(p["prediction"]))


def test_ensemble_validation_by_tier(client, queue, loaded, project):
    pid, _ = loaded
    assert client.post(f"/projects/{pid}/predict", json={"endpoints": ["logd"], "ensemble_n": 20}, headers=H("a")).status_code == 422  # free cap 10
    assert client.post(f"/projects/{pid}/predict", json={"endpoints": ["logd"], "ensemble_n": 7}, headers=H("a")).status_code == 422
    assert client.post(f"/projects/{pid}/predict", json={"endpoints": ["nope"], "ensemble_n": 5}, headers=H("a")).status_code == 422
    assert client.post(f"/projects/{pid}/predict", json={"endpoints": ["logd", "logd"]}, headers=H("a")).status_code == 422
    assert client.post(f"/projects/{pid}/predict", json={"endpoints": []}, headers=H("a")).status_code == 422
    r = client.post("/projects", json={"name": "edu"}, headers=H("edu")).json()["id"]
    assert client.post(f"/projects/{r}/predict", json={"endpoints": ["logd"], "ensemble_n": 20}, headers=H("edu")).status_code == 422  # no compounds
    client.post(f"/projects/{r}/compounds", json={"smiles": ["CCO"]}, headers=H("edu"))
    rr = client.post(f"/projects/{r}/predict", json={"endpoints": ["logd"], "ensemble_n": 20}, headers=H("edu"))
    assert rr.status_code == 202
    queue.wait(uuid.UUID(rr.json()["job_id"]))


def test_job_failure_is_generic(client, queue, app, loaded):
    from backend.jobs import deps

    class Bad:
        def __init__(self, base):
            self.base = base

        def __getattr__(self, n):
            return getattr(self.base, n)

        def predict(self, e, smiles, n):
            raise RuntimeError(f"boom {smiles}")

    base = app.dependency_overrides[deps.get_registry]()
    app.dependency_overrides[deps.get_registry] = lambda: Bad(base)
    pid, _ = loaded
    _, job = run_job(client, queue, pid, endpoints=["logd"])
    assert job["state"] == "failed" and "CCO" not in str(job) and job["error"] == "job failed (RuntimeError)"


def test_mismatched_registry_output_fails(client, queue, app, loaded):
    from backend.jobs import deps

    class Mismatch:
        def __init__(self, base):
            self.base = base

        def __getattr__(self, n):
            return getattr(self.base, n)

        def predict(self, e, smiles, n):
            return self.base.predict("sol", smiles, n)

    base = app.dependency_overrides[deps.get_registry]()
    app.dependency_overrides[deps.get_registry] = lambda: Mismatch(base)
    pid, _ = loaded
    _, job = run_job(client, queue, pid, endpoints=["logd"])
    assert job["state"] == "failed"


def test_user_b_cannot_touch_user_a(client, queue, loaded):
    pid, comps = loaded
    start, job = run_job(client, queue, pid)
    jid, pred_id = start["job_id"], job["prediction_ids"][0]
    assert client.get(f"/jobs/{jid}", headers=H("b")).status_code == 404
    assert client.get(f"/predictions/{pred_id}/uncertainty", headers=H("b")).status_code == 404
    assert client.get(f"/predictions/{pred_id}/uncertainty", headers=H("a")).status_code == 200
    assert client.post(f"/projects/{pid}/predict", json={"endpoints": ["logd"]}, headers=H("b")).status_code == 404
    for fmt in ("csv", "sdf", "pdf"):
        assert client.get(f"/projects/{pid}/export?format={fmt}", headers=H("b")).status_code == 404
    assert client.put(f"/projects/{pid}/ranking", json={"weights": [{"endpoint": "logd", "weight": 1, "higher_is_better": True}]}, headers=H("b")).status_code == 404
    assert client.get("/projects", headers=H("b")).json()["projects"] == []
    assert client.get(f"/projects/{pid}", headers=H("b")).status_code == 404
    assert client.delete(f"/projects/{pid}", headers=H("b")).status_code == 404
    assert client.get(f"/projects/{pid}", headers=H("a")).json()["compounds"][0]["predictions"]  # untouched


def test_unknown_job_404(client):
    assert client.get(f"/jobs/{uuid.uuid4()}", headers=H("a")).status_code == 404
    assert client.get("/jobs/not-a-uuid", headers=H("a")).status_code == 422


def test_uncertainty_detail(client, queue, loaded):
    pid, _ = loaded
    _, job = run_job(client, queue, pid)
    r = client.get(f"/predictions/{job['prediction_ids'][0]}/uncertainty", headers=H("a"))
    d = PredictionDetail.model_validate(r.json())
    assert d.fragments and d.nearest_training and d.prediction.method_note
    assert client.get(f"/predictions/{uuid.uuid4()}/uncertainty", headers=H("a")).status_code == 404


def test_uncertainty_409_when_model_version_changed(client, queue, app, loaded):
    from backend.jobs import deps
    from api_support import FakeRegistry

    pid, _ = loaded
    _, job = run_job(client, queue, pid)

    class New(FakeRegistry):
        version_id = uuid.uuid4()

    app.dependency_overrides[deps.get_registry] = lambda: New()
    assert client.get(f"/predictions/{job['prediction_ids'][0]}/uncertainty", headers=H("a")).status_code == 409


def test_models_public(client):
    r = client.get("/models")
    assert r.status_code == 200 and {m["endpoint"] for m in r.json()["models"]} == {"logd", "sol", "ames"}
    assert client.get("/models/logd").json()["default_range"] == [-2.0, 6.0]
    assert client.get("/models/unknown").status_code == 404
    assert client.get("/models/..%2Fx").status_code == 404
    assert client.get("/models/BAD NAME").status_code == 404


def test_registry_unavailable_503(app, client, monkeypatch):
    import sys

    from backend.jobs import deps

    del app.dependency_overrides[deps.get_registry]
    monkeypatch.setitem(sys.modules, "ml.registry", None)  # simulate the ml package being absent
    assert client.get("/models").status_code == 503


def test_real_untrained_registry_lists_no_models(app, client):
    from backend.jobs import deps

    del app.dependency_overrides[deps.get_registry]
    r = client.get("/models")
    assert r.status_code == 200 and r.json() == {"models": []}  # nothing is trained or published yet


# ---------- exports ----------


def seeded(client, queue, loaded):
    pid, comps = loaded
    run_job(client, queue, pid, endpoints=["logd", "ames"])
    client.post(f"/projects/{pid}/measurements", json={"compound_id": comps[0]["id"], "endpoint": "logd", "value": 0.5}, headers=H("a"))
    return pid


def test_export_csv(client, queue, loaded):
    pid = seeded(client, queue, loaded)
    r = client.get(f"/projects/{pid}/export?format=csv", headers=H("a"))
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    head, body = disclaimer()
    assert body in r.text and r.text.startswith("# ")
    rows = [ln for ln in r.text.splitlines() if not ln.startswith("# ")]
    assert rows[0].startswith("compound,label,canonical_smiles,ames_predicted") and len(rows) == 4
    assert "verdict" not in r.text.lower()


def test_export_csv_formula_injection(client, project):
    client.post(f"/projects/{project}/compounds", json={"format": "csv", "content": "name,smiles\n=HYPERLINK(1),CCO\n"}, headers=H("a"))
    r = client.get(f"/projects/{project}/export?format=csv", headers=H("a"))
    assert "'=HYPERLINK(1)" in r.text and ",=HYPERLINK" not in r.text


def test_export_sdf(client, queue, loaded):
    pid = seeded(client, queue, loaded)
    r = client.get(f"/projects/{pid}/export?format=sdf", headers=H("a"))
    assert r.status_code == 200
    from rdkit import Chem

    recs = [b for b in r.text.split("$$$$\n") if b.strip()]
    assert len(recs) == 3
    head, body = disclaimer()
    for rec in recs:
        assert body in rec and "<conformer_method>" in rec and "ETKDG" in rec and "computed (not experimental)" in rec
        assert rec.splitlines()[0].startswith("Metis compound") and "DISCLAIMER" in rec.splitlines()[0]
        m = Chem.MolFromMolBlock(rec.split("M  END")[0] + "M  END\n")
        assert m is not None and m.GetConformer().Is3D()
    assert "predicted" in recs[0] and "ensemble N=5" in recs[0]
    suppl = Chem.SDMolSupplier()
    suppl.SetData(r.text)
    mols = [m for m in suppl if m is not None]
    assert len(mols) == 3 and mols[0].GetProp("conformer_label").startswith("computed")


def test_export_pdf(client, queue, loaded):
    pid = seeded(client, queue, loaded)
    r = client.get(f"/projects/{pid}/export?format=pdf", headers=H("a"))
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and r.headers["content-type"] == "application/pdf"
    head, body = disclaimer()
    assert body[:40].encode("latin-1") in r.content  # footer text (uncompressed stream)


def test_export_bad_format(client, project):
    assert client.get(f"/projects/{project}/export?format=docx", headers=H("a")).status_code == 422
    assert client.get(f"/projects/{project}/export", headers=H("a")).status_code == 422


def test_export_empty_project(client, project):
    for fmt in ("csv", "sdf", "pdf"):
        assert client.get(f"/projects/{project}/export?format={fmt}", headers=H("a")).status_code == 200


# ---------- ranking endpoint ----------


def test_ranking_endpoint(client, queue, loaded):
    pid, comps = loaded
    run_job(client, queue, pid, endpoints=["logd", "ames"])
    cfg = {
        "weights": [{"endpoint": "logd", "weight": 2, "higher_is_better": True}, {"endpoint": "ames", "weight": 1, "higher_is_better": False}],
        "limits": [{"endpoint": "ames", "max": 0.5}],
        "lam": 0.25,
    }
    r = client.put(f"/projects/{pid}/ranking", json=cfg, headers=H("a"))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["persisted"] is True and j["stale_model_version"] is False and j["formula"] == "R_j = (1 - lam) S_j + lam C_j" and len(j["ranking"]) == 3
    ranks = [x["blended_rank"] for x in j["ranking"]]
    assert sorted(ranks)[0] == 1
    passers = [x for x in j["ranking"] if x["passes_limits"]]
    failers = [x for x in j["ranking"] if not x["passes_limits"]]
    assert all(p["blended_rank"] < f["blended_rank"] for p in passers for f in failers)
    assert all(f["limit_failures"][0]["note"].startswith("limit:") for f in failers)
    assert set(j["ranking"][0]) >= {"blended_rank", "property_rank", "breakdown", "property_score", "confidence_score"}
    # invalid configs
    assert client.put(f"/projects/{pid}/ranking", json={**cfg, "lam": 2}, headers=H("a")).status_code == 422
    bad = {"weights": [{"endpoint": "sol", "weight": 1, "higher_is_better": True}]}
    assert client.put(f"/projects/{pid}/ranking", json=bad, headers=H("a")).status_code == 422  # no predictions yet for sol
    # the last valid config is persisted and returned; the rejected one did not overwrite it
    g = client.get(f"/projects/{pid}/ranking", headers=H("a")).json()
    assert g["persisted"] is True and g["config"]["lam"] == 0.25 and g["config"]["weights"][0]["endpoint"] == "logd"
    assert client.get(f"/projects/{pid}/ranking", headers=H("b")).status_code == 404


def test_ranking_needs_median_width(client, queue, app, loaded):
    from backend.jobs import deps
    from api_support import FakeRegistry

    pid, _ = loaded
    run_job(client, queue, pid, endpoints=["logd"])

    class NoWidth(FakeRegistry):
        def _card(self, e):
            c = super()._card(e)
            c.benchmark.pop("median_calibration_width", None)
            return c

    app.dependency_overrides[deps.get_registry] = lambda: NoWidth()
    r = client.put(f"/projects/{pid}/ranking", json={"weights": [{"endpoint": "logd", "weight": 1, "higher_is_better": True}]}, headers=H("a"))
    assert r.status_code == 422 and "median calibration width" in r.text


def _put_logd(client, pid):
    return client.put(f"/projects/{pid}/ranking", json={"weights": [{"endpoint": "logd", "weight": 1, "higher_is_better": True}]}, headers=H("a"))


def test_ranking_config_none_before_first_put(client, project):
    assert client.get(f"/projects/{project}/ranking", headers=H("a")).json() == {"config": None, "persisted": False}


def test_ranking_reads_card_field_by_n(client, queue, app, loaded):
    from backend.jobs import deps
    from api_support import FakeRegistry

    pid, _ = loaded
    run_job(client, queue, pid, endpoints=["logd"], ensemble_n=5)

    class Card(FakeRegistry):
        def _card(self, e):
            c = super()._card(e)
            c.benchmark.pop("median_calibration_width", None)
            return c.model_copy(update={"median_calibration_width_by_n": {"5": 4.0}} if e == "logd" else {})

    app.dependency_overrides[deps.get_registry] = lambda: Card()
    r = _put_logd(client, pid)
    assert r.status_code == 200, r.text
    assert r.json()["ranking"][0]["breakdown"][0]["confidence"] == pytest.approx(1 - 1.0 / (2 * 4.0))  # fake interval width is 1.0

    class OtherN(Card):  # width only for another N -> clear 422
        def _card(self, e):
            c = super()._card(e)
            return c.model_copy(update={"median_calibration_width_by_n": {"20": 4.0}}) if e == "logd" else c

    app.dependency_overrides[deps.get_registry] = lambda: OtherN()
    r = _put_logd(client, pid)
    assert r.status_code == 422 and "median calibration width" in r.text


def test_ranking_uses_each_predictions_own_n(client, queue, app, loaded):
    from backend.jobs import deps
    from api_support import FakeRegistry

    pid, comps = loaded
    run_job(client, queue, pid, endpoints=["logd"], ensemble_n=3)
    run_job(client, queue, pid, endpoints=["logd"], ensemble_n=10)  # newest per (compound, endpoint) wins -> all N=10

    class Card(FakeRegistry):
        def _card(self, e):
            c = super()._card(e)
            return c.model_copy(update={"median_calibration_width_by_n": {"3": 100.0, "10": 1.0}}) if e == "logd" else c

    app.dependency_overrides[deps.get_registry] = lambda: Card()
    r = _put_logd(client, pid).json()
    assert all(x["breakdown"][0]["confidence"] == pytest.approx(0.5) for x in r["ranking"])  # 1 - 1/(2*1)


def test_ranking_flags_stale_model_version(client, queue, app, loaded):
    import uuid as _u
    from backend.jobs import deps
    from api_support import FakeRegistry

    pid, _ = loaded
    run_job(client, queue, pid, endpoints=["logd"])
    assert _put_logd(client, pid).json()["stale_model_version"] is False

    class Newer(FakeRegistry):
        version_id = _u.UUID("22222222-2222-2222-2222-222222222222")

    app.dependency_overrides[deps.get_registry] = lambda: Newer()
    j = _put_logd(client, pid).json()
    assert j["stale_model_version"] is True and len(j["stale_predictions"]) == 3
    assert all("stale" in x["reason"] for x in j["stale_predictions"])
    assert all(x["stale_endpoints"] == ["logd"] for x in j["ranking"])


def test_per_user_job_quota_429(client, app, loaded):
    import threading
    from backend.jobs import deps
    from backend.jobs.queue import MAX_ACTIVE_PER_USER, InProcessJobQueue
    from api_support import FakeRegistry

    pid, _ = loaded
    ev = threading.Event()

    class Slow(FakeRegistry):
        def predict(self, *a, **k):
            ev.wait(10)
            return super().predict(*a, **k)

    q = InProcessJobQueue(max_workers=1)
    app.dependency_overrides[deps.get_registry] = lambda: Slow()
    app.dependency_overrides[deps.get_queue] = lambda: q
    codes = [client.post(f"/projects/{pid}/predict", json={"endpoints": ["logd"]}, headers=H("a")).status_code for _ in range(MAX_ACTIVE_PER_USER + 3)]
    other = client.post("/projects", json={"name": "b"}, headers=H("b")).json()["id"]
    client.post(f"/projects/{other}/compounds", json={"smiles": ["CCO"]}, headers=H("b"))
    code_b = client.post(f"/projects/{other}/predict", json={"endpoints": ["logd"]}, headers=H("b")).status_code
    ev.set()
    q.shutdown()  # let every queued job finish before the fixture truncates the tables (else: deadlock)
    assert codes[:MAX_ACTIVE_PER_USER] == [202] * MAX_ACTIVE_PER_USER and codes[MAX_ACTIVE_PER_USER:] == [429] * 3
    assert code_b == 202  # quota is per user


def test_job_queue_evicts_finished_behind_unfinished():
    import threading
    import uuid as _u
    from backend.jobs.queue import MAX_TRACKED, InProcessJobQueue

    q, ev, u, p = InProcessJobQueue(max_workers=2), threading.Event(), _u.uuid4(), _u.uuid4()
    for _ in range(2):
        q.submit(u, p, 1, 0, lambda pr: ev.wait(10))
    for _ in range(MAX_TRACKED + 50):
        q.submit(u, p, 1, 0, lambda pr: None)
    n = len(q._jobs)
    ev.set()
    assert n <= MAX_TRACKED


def test_compound_sandbox_busy_is_503(client, project, monkeypatch):
    from backend.compounds import sandbox

    taken = [sandbox._slots.acquire(blocking=False) for _ in range(sandbox.MAX_CONCURRENT)]
    try:
        r = client.post(f"/projects/{project}/compounds", json={"smiles": ["CCO"]}, headers=H("a"))
        assert r.status_code == 503 and "busy" in r.text
    finally:
        for t in taken:
            if t:
                sandbox._slots.release()
    assert client.post(f"/projects/{project}/compounds", json={"smiles": ["CCO"]}, headers=H("a")).status_code == 201
