"""Time and size limits added for F-145/F-146/F-162: deadline-limited SMARTS matching, bounded embedding, stuck-run slots."""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from rdkit import Chem

from leadstudio import matching
from leadstudio.engine import geometry
from leadstudio.profiles import MAX_SMARTS_ATOMS, check_smarts
from leadstudio.schema import RunConfig
from leadstudio.server.core import MAX_ABANDONED, Rejected, RunManager, Settings


def _grid(w: int, h: int) -> Chem.Mol:
    m = Chem.RWMol()
    ids = {(i, j): m.AddAtom(Chem.Atom(6)) for i in range(w) for j in range(h)}
    for i in range(w):
        for j in range(h):
            if i + 1 < w:
                m.AddBond(ids[i, j], ids[i + 1, j], Chem.BondType.SINGLE)
            if j + 1 < h:
                m.AddBond(ids[i, j], ids[i, j + 1], Chem.BondType.SINGLE)
    mol = m.GetMol()
    Chem.SanitizeMol(mol)
    return mol


def test_matching_gives_up_at_the_deadline_and_still_finds_ordinary_matches():
    hard = Chem.MolFromSmarts("~".join(["[#6]"] * 22) + "~[#6;R0;R1]")
    t = time.monotonic()
    res = matching.match(_grid(6, 10), hard, seconds=0.2)
    assert time.monotonic() - t < 3 and res.timed_out and not res.matches
    ok = matching.match(Chem.MolFromSmiles("CCO"), Chem.MolFromSmarts("[OX2H]"), seconds=1.0)
    assert ok.matches == ((2,),) and not ok.timed_out


def test_smarts_complexity_is_capped_at_validation():
    assert check_smarts("~".join(["[#6]"] * (MAX_SMARTS_ATOMS + 1))) is not None
    assert check_smarts("[$(C=O)].[$(CN)].[$(CO)].[$(CS)].[$(CF)]") is not None
    assert check_smarts("[CX3](=O)[OX2H1]") is None


def test_embedding_a_hopeless_molecule_returns_within_the_budget():
    smi = Chem.MolToSmiles(_grid(4, 4))
    t = time.monotonic()
    geometry.embed_raw(smi)
    assert time.monotonic() - t < geometry.EMBED_BUDGET_S + 2


def test_stuck_runs_are_bounded_after_their_slots_are_handed_back():
    release = threading.Event()

    class Hung:
        def run(self, run_id, config):
            yield {"type": "run_start", "payload": {}}
            release.wait(30)

    mgr = RunManager(Settings(max_concurrent=1, run_timeout=0.2, per_user_active=50, max_queue=50, orphan_grace=0), Hung(), [])
    cfg = RunConfig(seed_smiles="CCO")
    try:
        for i in range(MAX_ABANDONED):
            mgr.submit(uuid.uuid4(), cfg.model_copy(update={"rng_seed": i}), f"r{i}")
            deadline = time.monotonic() + 5
            while mgr.pool.abandoned <= i and time.monotonic() < deadline:
                time.sleep(0.05)
        assert mgr.pool.abandoned == MAX_ABANDONED
        with pytest.raises(Rejected) as e:
            mgr.submit(uuid.uuid4(), cfg, "late")
        assert e.value.status_code == 503
    finally:
        release.set()
        mgr.pool.shutdown(wait=True)
    assert mgr.pool.abandoned == 0
