"""Wave 5 (lead2): policy adapters on runs, profile edit allow-lists, client seed 3D blocks.

torch is an optional dependency and is NOT installed in every environment. The adapter tests here use fakes for the
policy itself (a stand-in `policy_factory`, a stand-in child); the one test that loads real weights is marked
`importorskip("torch")` and is SKIPPED where torch is missing. What the fakes cannot show is the final torch.load, so
a static test pins that every torch.load in the adapter code passes weights_only=True.
"""

from __future__ import annotations

import ast
import hashlib
import json
import random
import secrets
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import H, make_app
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from rdkit import Chem
from rdkit.Chem import AllChem

from leadstudio import profiles
from leadstudio.adapt import store
from leadstudio.engine import geometry
from leadstudio.engine.fakes import make_fake_engine
from leadstudio.profile_scorer import AllowlistRegistry
from leadstudio.schema import RunConfig
from leadstudio.server import heavychild, procutil, routes, runchild
from leadstudio.server.core import RunManager, Settings
from leadstudio.server.stages import Services, StageEngine

NITRO = "CCOC(=O)c1ccc(cc1)[N+](=O)[O-]"
SEED_ID = "00000000-0000-0000-0000-000000000000"
FAST = Settings(force_mock=True, mock_delay=0.0, replay_delay=0.0, runs_per_minute=1000, per_user_active=20)


# ------------------------------------------------------------------------------------------------ adapter fixtures
@pytest.fixture
def adapter_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_CACHE_DIR", str(tmp_path))
    return tmp_path


def make_adapter(owner: str, payload: bytes = b"stand-in weights, not a checkpoint") -> tuple[str, str]:
    """Write an adapter the way adapt.store lays it out (without torch). Returns (adapter_id, sha256)."""
    aid = secrets.token_hex(8)
    d = store.owner_dir(owner)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{aid}.pt").write_bytes(payload)
    (d / f"{aid}.json").write_text(json.dumps({"adapter_id": aid, "owner": owner, "op_id_hash": "x"}))
    return aid, hashlib.sha256(payload).hexdigest()


def post_run(c, who, **extra):
    return c.post("/lead/api/runs", json={"seed_smiles": NITRO, **extra}, headers=H(who))


def live_client(factory, users, monkeypatch, torch_available=True):
    """Mock-mode app flipped to 'live' (no engine is built) whose run manager records what it was asked to run."""
    app = make_app(factory, users, FAST)
    st = app.state.lead
    st.engine = object()
    monkeypatch.setattr(routes, "_torch_available", lambda: torch_available)
    seen: list[dict] = []

    def submit(owner, cfg, run_id, profile=None, adapter=None):
        seen.append({"owner": owner, "cfg": cfg, "profile": profile, "adapter": adapter})
        return SimpleNamespace(run_id=run_id, source="live")

    monkeypatch.setattr(st.mgr, "submit", submit)
    return TestClient(app), seen


# ------------------------------------------------------------------------------------------- adapter_id: the route
def test_unknown_malformed_and_foreign_adapter_ids_are_one_404(factory, users, adapter_dir):
    c = TestClient(make_app(factory, users, FAST))
    mine, _ = make_adapter(str(users["a"].id))
    assert post_run(c, "a", adapter_id=secrets.token_hex(8)).status_code == 404  # well-formed, does not exist
    for bad in ("../../etc/passwd", "ABCDEF0123456789", "x" * 64, "1234", "a.pt"):
        assert post_run(c, "a", adapter_id=bad).status_code == 404, bad
    foreign = post_run(c, "b", adapter_id=mine)  # user a's adapter, asked for by user b
    assert foreign.status_code == 404
    assert foreign.json() == post_run(c, "b", adapter_id=secrets.token_hex(8)).json()  # indistinguishable


def test_adapter_needs_the_live_engine_and_torch(factory, users, adapter_dir, monkeypatch):
    aid, _ = make_adapter(str(users["a"].id))
    mock = TestClient(make_app(factory, users, FAST))
    assert post_run(mock, "a", adapter_id=aid).status_code == 503  # mock mode
    c, seen = live_client(factory, users, monkeypatch, torch_available=False)
    assert post_run(c, "a", adapter_id=aid).status_code == 503  # live, but no torch: the existing 503 stays
    assert seen == []


def test_own_adapter_is_pinned_by_owner_id_and_sha256_and_sent_to_the_run(factory, users, adapter_dir, monkeypatch):
    uid = str(users["a"].id)
    aid, digest = make_adapter(uid)
    c, seen = live_client(factory, users, monkeypatch)
    r = post_run(c, "a", adapter_id=aid)
    assert r.status_code == 200 and r.json()["source"] == "live"
    assert seen[0]["adapter"] == {"owner": uid, "adapter_id": aid, "sha256": digest}
    assert seen[0]["cfg"].adapter_id == aid
    assert post_run(c, "b", adapter_id=aid).status_code == 404  # still not b's
    assert len(seen) == 1
    seen.clear()
    assert post_run(c, "a").status_code == 200 and seen[0]["adapter"] is None  # no adapter asked: none sent


def test_adapter_is_pharmacology_only_and_an_oversized_file_is_unknown(factory, users, adapter_dir, monkeypatch):
    aid, _ = make_adapter(str(users["a"].id))
    c, _seen = live_client(factory, users, monkeypatch)
    assert post_run(c, "a", adapter_id=aid, stage="binding", target_id="x").status_code == 422
    monkeypatch.setattr(store, "MAX_WEIGHTS_BYTES", 5)
    assert post_run(c, "a", adapter_id=aid).status_code == 404


# ---------------------------------------------------------------------------------------- adapter_id: weights store
def test_read_weights_is_pinned_to_the_sha256_and_bounded(adapter_dir, monkeypatch):
    aid, digest = make_adapter("u1", b"abcdef")
    assert store.read_weights("u1", aid) == b"abcdef"
    assert store.read_weights("u1", aid, digest) == b"abcdef"
    assert store.read_weights("u1", aid, "0" * 64) is None  # swapped file
    assert store.read_weights("u2", aid) is None  # other owner's directory
    assert store.read_weights("u1", "../" + aid) is None
    assert store.weights_sha256("u1", aid) == digest
    monkeypatch.setattr(store, "MAX_WEIGHTS_BYTES", 5)
    assert store.read_weights("u1", aid) is None and store.weights_sha256("u1", aid) is None


def test_every_torch_load_of_an_adapter_is_weights_only():
    """No pickle of untrusted data: a static check of all torch.load calls under leadstudio/."""
    calls = 0
    for path in Path(store.__file__).parents[1].rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "load":
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "torch":
                    calls += 1
                    kw = {k.arg: k.value for k in node.keywords}
                    ok = isinstance(kw.get("weights_only"), ast.Constant) and kw["weights_only"].value is True
                    assert ok, f"{path.name}:{node.lineno} torch.load without weights_only=True"
    assert calls >= 2


def test_real_adapter_loads_only_with_the_matching_hash():
    """Needs torch (SKIPPED where it is not installed): a real saved adapter, hash-pinned load, tamper -> fallback."""
    pytest.importorskip("torch")
    import os
    import tempfile

    from leadstudio.adapt import policy_factory
    from leadstudio.policy.dqn import DQNPolicy

    ops = ["a", "b", "c"]
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["STUDIO_CACHE_DIR"] = tmp
        try:
            meta = store.save("u1", DQNPolicy(ops, checkpoint=None), n_steps=5, base_checkpoint="x", base_kind="y",
                              evaluation={}, training={})
            aid = meta["adapter_id"]
            digest = store.weights_sha256("u1", aid)
            good = policy_factory.load_adapted_policy(aid, "u1", ops, sha256=digest)
            assert good.adapter_info["used"] is True
            bad = policy_factory.load_adapted_policy(aid, "u1", ops, sha256="0" * 64)
            assert bad.adapter_info["used"] is False
            path = store.weights_path("u1", aid)
            path.write_bytes(path.read_bytes() + b"x")
            tampered = policy_factory.load_adapted_policy(aid, "u1", ops, sha256=digest)
            assert tampered.adapter_info["used"] is False
        finally:
            os.environ.pop("STUDIO_CACHE_DIR", None)


# --------------------------------------------------------------------------------------- adapter_id: the run child
class FakeChild:
    """Stands in for procutil.Child: records what the supervisor sends and answers with a finished run."""

    sent: list[bytes] = []

    @classmethod
    def spawn(cls, module, argv=()):
        return cls()

    def send(self, data, timeout):
        FakeChild.sent.append(data)

    def read_line(self, timeout):
        return b'{"type":"run_end","payload":{}}'

    def kill(self):
        pass


def test_manager_sends_the_pin_to_the_child_and_never_replays_an_adapter_run(monkeypatch):
    import time
    import uuid

    FakeChild.sent = []
    monkeypatch.setattr(procutil.Child, "spawn", FakeChild.spawn)
    mgr = RunManager(Settings(orphan_grace=0), object(), [])
    pin = {"owner": "o", "adapter_id": "ab" * 8, "sha256": "f" * 64}
    cfg = RunConfig(seed_smiles="CCO", adapter_id=pin["adapter_id"])
    run = mgr.submit(uuid.uuid4(), cfg, "r1", adapter=pin)
    for _ in range(100):
        if run.finished:
            break
        time.sleep(0.05)
    mgr.shutdown()
    assert run.cache_key is None and json.loads(FakeChild.sent[0])["adapter"] == pin
    plain = RunManager(Settings(orphan_grace=0), object(), [])
    FakeChild.sent = []
    r2 = plain.submit(uuid.uuid4(), RunConfig(seed_smiles="CCO"), "r2")
    for _ in range(100):
        if r2.finished:
            break
        time.sleep(0.05)
    plain.shutdown()
    assert json.loads(FakeChild.sent[0])["adapter"] is None


def _fake_factory(monkeypatch, used: bool, reason: str = "ok"):
    calls: list[tuple] = []

    def load_adapted_policy(adapter_id, owner, op_ids=None, sha256=None):
        calls.append((adapter_id, owner, sha256))
        return SimpleNamespace(adapter_info={"used": used, "reason": reason}, rank=lambda s, c: [0.0] * len(c))

    mod = types.ModuleType("leadstudio.adapt.policy_factory")
    mod.load_adapted_policy = load_adapted_policy  # type: ignore[attr-defined]  # test stand-in
    monkeypatch.setitem(sys.modules, "leadstudio.adapt.policy_factory", mod)
    return calls


def test_child_applies_the_adapter_to_a_copy_and_reports_it(monkeypatch):
    calls = _fake_factory(monkeypatch, used=True)
    base = StageEngine(make_fake_engine(), Services())
    spec = {"adapter": {"owner": "o", "adapter_id": "ab" * 8, "sha256": "f" * 64}}
    eng = runchild.assemble(base, spec)
    assert calls == [("ab" * 8, "o", "f" * 64)]  # the pin reaches the loader
    assert eng.base.policy is not base.base.policy  # a copy: the shared engine keeps its own policy
    assert "in use" in eng.base.policy.note
    assert eng.base.policy.update([("x",)]) is None  # frozen: a run never trains the adapter
    assert runchild.assemble(base, {}) is base  # nothing asked, nothing changed


def test_child_says_so_when_the_adapter_could_not_be_used(monkeypatch):
    _fake_factory(monkeypatch, used=False, reason="adapter file changed or too large")
    eng = runchild.assemble(StageEngine(make_fake_engine(), Services()), {"adapter": {"owner": "o", "adapter_id": "a" * 16, "sha256": "0" * 64}})
    assert "not used" in eng.base.policy.note and "changed" in eng.base.policy.note


# ------------------------------------------------------------------------------------- profile edit allow-list
def test_allowlist_registry_confines_the_edits_and_leaves_the_registry_alone():
    from leadstudio.registry import get_registry

    reg = get_registry()
    smi = "CCOC(=O)c1ccc(cc1)N"
    ids = sorted({op.id for op, _ in reg.applicable(smi)})
    assert len(ids) >= 3
    allowed = ids[:2]
    view = AllowlistRegistry(reg, allowed)
    got = {op.id for op, _ in view.applicable(smi)}
    assert got == set(allowed)
    assert {op.id for op in view.all()} == set(allowed)
    assert view.get(allowed[0]).id == allowed[0]
    with pytest.raises(KeyError):
        view.get(ids[2])
    assert {op.id for op, _ in reg.applicable(smi)} == set(ids)  # the shared registry is untouched
    assert AllowlistRegistry(reg, ["no_such_edit"]).applicable(smi) == []
    assert view._ops is reg._ops or view._ops == reg._ops  # other attributes read through to the real registry


def test_assemble_applies_the_profile_allowlist_to_a_copy_only():
    base = StageEngine(make_fake_engine(), Services())
    body = {"name": "p", "edit_allowlist": []}
    assert runchild.assemble(base, {"profile": body}).base.registry is base.base.registry  # empty: no restriction
    from leadstudio.registry import get_registry

    first = get_registry().all()[0].id
    eng = runchild.assemble(base, {"profile": {**body, "edit_allowlist": [first]}})
    assert isinstance(eng.base.registry, AllowlistRegistry) and eng.base.registry.allowed == {first}
    assert not isinstance(base.base.registry, AllowlistRegistry)


def test_profile_allowlist_validation(monkeypatch):
    ok = profiles.ProfileBody.model_validate({"name": "p", "edit_allowlist": ["atom_sub_n", "atom_sub_n", "atom_sub_o"]})
    assert ok.edit_allowlist == ["atom_sub_n", "atom_sub_o"]  # duplicates collapse
    for bad in (["no_such_edit"], ["atom_sub_n", "x" * 81], [1], [f"id{i}" for i in range(301)]):
        with pytest.raises(ValidationError):
            profiles.ProfileBody.model_validate({"name": "p", "edit_allowlist": bad})
    monkeypatch.setattr(profiles, "known_edit_ids", lambda: None)  # cannot tell what is real: refuse, do not store
    with pytest.raises(ValidationError):
        profiles.ProfileBody.model_validate({"name": "p", "edit_allowlist": ["atom_sub_n"]})
    assert profiles.ProfileBody.model_validate({"name": "p", "edit_allowlist": []}).edit_allowlist == []


def test_profile_route_rejects_unknown_edit_names_at_save_time(factory, users):
    c = TestClient(make_app(factory, users, FAST))
    body = {"name": "p", "objectives": {}, "avoid_smarts": [], "edit_allowlist": ["atom_sub_n", "nope"]}
    r = c.post("/lead/profiles", json=body, headers=H("a"))
    assert r.status_code == 422 and "unknown edit id" in r.text
    assert c.post("/lead/profiles", json={**body, "edit_allowlist": ["atom_sub_n"]}, headers=H("a")).status_code == 201


# ------------------------------------------------------------------------------------------------- seed_molblock
def make_block(smiles=NITRO, shuffle=False, flat=False, shift=0.0):
    mol = Chem.MolFromSmiles(smiles)
    if flat:
        AllChem.Compute2DCoords(mol)
        return Chem.MolToMolBlock(mol)
    _block, m3 = geometry.embed_3d(smiles)
    if shift:
        conf = m3.GetConformer()
        for i in range(m3.GetNumAtoms()):
            p = conf.GetAtomPosition(i)
            conf.SetAtomPosition(i, (p.x + shift, p.y, p.z))
    if shuffle:
        order = list(range(m3.GetNumAtoms()))
        random.Random(1).shuffle(order)
        m3 = Chem.RenumberAtoms(m3, order)
    return Chem.MolToMolBlock(m3)


def positions(block):
    m = Chem.MolFromMolBlock(block)
    c = m.GetConformer()
    return [(a.GetAtomicNum(), round(c.GetAtomPosition(a.GetIdx()).x, 3), round(c.GetAtomPosition(a.GetIdx()).y, 3)) for a in m.GetAtoms()]


def test_seed_block_is_rewritten_in_the_seed_atom_order():
    seed = Chem.MolToSmiles(Chem.MolFromSmiles(NITRO))
    out = heavychild.op_seed_block({"smiles": seed, "molblock": make_block(shuffle=True, shift=5.0)})["molblock"]
    got, want = Chem.MolFromMolBlock(out), Chem.MolFromSmiles(seed)
    assert [a.GetAtomicNum() for a in got.GetAtoms()] == [a.GetAtomicNum() for a in want.GetAtoms()]
    assert geometry.same_graph(want, got)
    xs = [p[1] for p in positions(out)]
    assert sum(xs) / len(xs) > 3.0  # the client's coordinates, not a fresh embedding
    again = heavychild.op_seed_block({"smiles": seed, "molblock": out})["molblock"]
    assert positions(again) == positions(out)  # stable


@pytest.mark.parametrize(
    "block",
    [
        make_block(flat=True),  # 2D
        make_block("CCOC(=O)c1ccc(cc1)C#N"),  # another molecule
        make_block("CCO"),
        "garbage",
        "",
        "x" * 200_000,
        "A\x00B\nM  END",
        "\n\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n",
    ],
)
def test_seed_block_that_is_not_the_3d_seed_is_refused(block):
    with pytest.raises(HTTPException) as e:
        heavychild.op_seed_block({"smiles": Chem.MolToSmiles(Chem.MolFromSmiles(NITRO)), "molblock": block})
    assert e.value.status_code == 422


def test_seed_block_route_validates_and_stores_the_rewritten_block(factory, users):
    app = make_app(factory, users, FAST)
    c = TestClient(app)
    r = post_run(c, "a", seed_molblock=make_block(shuffle=True))
    assert r.status_code == 200
    stored = app.state.lead.mgr.runs[r.json()["run_id"]].config.seed_molblock
    assert stored and stored != make_block(shuffle=True)  # re-written, not echoed
    for bad in (make_block(flat=True), make_block("CCO"), "not a block", "x" * 150_001):
        assert post_run(c, "a", seed_molblock=bad).status_code == 422, bad[:20]
    plain = post_run(c, "a")
    assert app.state.lead.mgr.runs[plain.json()["run_id"]].config.seed_molblock is None


def test_geometry_seed_from_block_never_raises_and_ignores_open_stereo():
    assert geometry.seed_from_block("CCO", "") is None
    assert geometry.seed_from_block("not a smiles", make_block("CCO")) is None
    chiral = "C[C@H](N)C(=O)O"
    assert geometry.seed_from_block("CC(N)C(=O)O", make_block(chiral)) is not None  # seed leaves stereo open


# --------------------------------------------------------------------------- live: the whole stack, real children
@pytest.fixture
def live(factory, users):
    app = make_app(
        factory, users, Settings(mock_delay=0.0, replay_delay=0.0, runs_per_minute=1000, per_user_active=10)
    )
    assert app.state.lead.engine is not None, "the real engine failed to build"
    return TestClient(app)


def events_of(c, who, **extra):
    body = {"seed_smiles": NITRO, "generations": 2, "population_size": 6, "rng_seed": 3, **extra}
    r = c.post("/lead/api/runs", json=body, headers=H(who))
    assert r.status_code == 200, r.text
    text = c.get(f"/lead/api/runs/{r.json()['run_id']}/events", headers=H(who)).text
    return [json.loads(ln[6:]) for ln in text.splitlines() if ln.startswith("data: ")]


def test_live_run_only_proposes_the_profile_allowlisted_edits(live):
    from leadstudio.registry import get_registry

    ids = sorted({op.id for op, _ in get_registry().applicable(NITRO)})
    allowed = ids[:2]
    pid = live.post("/lead/profiles", json={"name": "p", "objectives": {}, "avoid_smarts": [], "edit_allowlist": allowed},
                    headers=H("a")).json()["id"]
    evs = events_of(live, "a", profile_id=pid, use_crossover=False, policy_guidance=0.0)
    assert evs[-1]["type"] == "run_end"
    used = {e["payload"]["edit"]["edit_id"] for e in evs if e["type"] == "candidate"}
    assert used and used <= set(allowed)
    free = {e["payload"]["edit"]["edit_id"] for e in events_of(live, "a", use_crossover=False, policy_guidance=0.0) if e["type"] == "candidate"}
    assert free - set(allowed)  # without the profile other edits do appear


def test_live_run_starts_from_the_supplied_seed_geometry(live):
    block = make_block(shift=7.0)
    evs = events_of(live, "a", seed_molblock=block)
    seed_block = evs[0]["payload"]["seed_molblock"]
    xs = [p[1] for p in positions(seed_block)]
    assert sum(xs) / len(xs) > 4.0  # the run's seed sits where the client put it, not at the origin an embedding gives
    plain = events_of(live, "a")[0]["payload"]["seed_molblock"]
    mean_x = sum(p[1] for p in positions(plain)) / len(positions(plain))
    assert abs(mean_x) < 3.0
