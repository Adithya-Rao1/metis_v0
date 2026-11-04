"""web/js/ranking.js against an independent Python implementation and hand-computed cases (spec 7)."""
import json
import math
import random

import pytest

INFOS = {
    "sol": dict(kind="regression", range=[-7.0, 0.0], cal_width=1.6),
    "perm": dict(kind="regression", range=[-7.0, -4.0], cal_width=1.4),
    "herg": dict(kind="classification", range=[0, 1], cal_width=None),
    "ames": dict(kind="classification", range=[0, 1], cal_width=None),
}


def H(p):
    return 0.0 if p <= 0 or p >= 1 else -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


def py_rank(compounds, cfg, infos):
    W = sum(w["weight"] for w in cfg["weights"])
    lam = cfg["lam"]
    out = []
    for idx, c in enumerate(compounds):
        S = C = 0.0
        for w in cfg["weights"]:
            p, info = c["predictions"][w["endpoint"]], infos[w["endpoint"]]
            if info["kind"] == "classification":
                d = p["probability"] if w["higher_is_better"] else 1 - p["probability"]
                cc = 1 - H(p["probability"])
            else:
                lo, hi = info["range"]
                t = min(1, max(0, (p["value"] - lo) / (hi - lo)))
                d = t if w["higher_is_better"] else 1 - t
                cc = max(0.0, 1 - (p["interval_high"] - p["interval_low"]) / (2 * info["cal_width"]))
            S += w["weight"] * d / W
            C += w["weight"] * cc / W
        fails = False
        for lim in cfg["limits"]:
            p, info = c["predictions"][lim["endpoint"]], infos[lim["endpoint"]]
            if info["kind"] == "classification":
                v = p["probability"]
            elif lim["use_unfavorable_bound"]:
                if lim["min"] is not None and lim["max"] is None:
                    v = p["interval_low"]
                elif lim["max"] is not None and lim["min"] is None:
                    v = p["interval_high"]
                else:
                    v = p["interval_low"] if lim["min"] - p["interval_low"] >= p["interval_high"] - lim["max"] else p["interval_high"]
            else:
                v = p["value"]
            fails = fails or (lim["min"] is not None and v < lim["min"]) or (lim["max"] is not None and v > lim["max"])
        out.append(dict(id=c["id"], idx=idx, S=S, C=C, R=(1 - lam) * S + lam * C, passes=not fails))
    for key, name in (("R", "blendedRank"), ("S", "propertyOnlyRank")):
        for rank, r in enumerate(sorted(out, key=lambda r: (not r["passes"], -r[key], r["idx"])), 1):
            r[name] = rank
    return out


def mk(i, sol, herg, ames=0.1, perm=(-5.0, -5.6, -4.4)):
    s = lambda v, w: dict(value=v, interval_low=round(v - w / 2, 6), interval_high=round(v + w / 2, 6))
    return dict(id=i, predictions=dict(
        sol=s(sol[0], sol[1]), perm=dict(value=perm[0], interval_low=perm[1], interval_high=perm[2]),
        herg=dict(value=herg, probability=herg), ames=dict(value=ames, probability=ames)))


@pytest.fixture()
def js(make_page):
    page = make_page()
    page.goto(page.base + "/auth.html")
    page.add_script_tag(type="module", content="import * as R from '/js/ranking.js'; window.R = R; window.RANKING_READY = true;")
    page.wait_for_function("window.RANKING_READY === true")
    return lambda cps, cfg, infos: page.evaluate("([c, g, i]) => R.scoreAll(c, g, i)", [cps, cfg, infos])


def test_hand_computed_case(js):
    # Hand computation (independent of both implementations).
    # Compound A: solubility -3.5 [-4.3,-2.7] -> d = 3.5/7 = 0.5, width 1.6 -> c = 1 - 1.6/3.2 = 0.5
    #             hERG 0.2 (lower better) -> d = 0.8, c = 1 - H(0.2) = 1 - 0.7219280948873623 = 0.2780719051126377
    # weights 8 and 9, W = 17, lambda = 0.25
    S = (8 * 0.5 + 9 * 0.8) / 17
    C = (8 * 0.5 + 9 * 0.2780719051126377) / 17
    assert round(S, 9) == 0.658823529 and round(C, 9) == 0.382508656
    R = 0.75 * S + 0.25 * C
    assert round(R, 9) == 0.589744811
    cfg = dict(lam=0.25, limits=[], weights=[dict(endpoint="sol", weight=8, higher_is_better=True), dict(endpoint="herg", weight=9, higher_is_better=False)])
    rows = js([mk("A", (-3.5, 1.6), 0.2)], cfg, INFOS)
    assert rows[0]["S"] == pytest.approx(S, abs=1e-12) and rows[0]["C"] == pytest.approx(C, abs=1e-12) and rows[0]["R"] == pytest.approx(R, abs=1e-12)
    assert sum(p["share"] for p in rows[0]["parts"]) == pytest.approx(R, abs=1e-12)


def test_confidence_edge_cases(js):
    cfg = dict(lam=1.0, limits=[], weights=[dict(endpoint="sol", weight=1, higher_is_better=True)])
    wide = js([mk("w", (-3.0, 9.0), 0.5)], cfg, INFOS)[0]  # width >= 2 * median -> floored at 0
    assert wide["C"] == 0
    cfg2 = dict(lam=1.0, limits=[], weights=[dict(endpoint="herg", weight=1, higher_is_better=True)])
    assert js([mk("a", (-3, 1), 0.5)], cfg2, INFOS)[0]["C"] == pytest.approx(0.0, abs=1e-12)  # p = 0.5 -> entropy 1 bit
    assert js([mk("a", (-3, 1), 1.0)], cfg2, INFOS)[0]["C"] == pytest.approx(1.0, abs=1e-12)  # certain


def test_limit_failers_rank_below_passers_and_unfavorable_bound(js):
    cfg = dict(lam=0.0, weights=[dict(endpoint="sol", weight=1, higher_is_better=True)],
               limits=[dict(endpoint="herg", min=None, max=0.30, use_unfavorable_bound=False)])
    best_but_fails = mk("fails", (-0.5, 0.2), 0.9)
    worse_passes = mk("passes", (-6.0, 0.2), 0.1)
    rows = {r["id"]: r for r in js([best_but_fails, worse_passes], cfg, INFOS)}
    assert rows["passes"]["blendedRank"] == 1 and rows["fails"]["blendedRank"] == 2 and not rows["fails"]["passes"]
    assert rows["fails"]["propertyOnlyRank"] == 2
    # unfavorable bound: point -4.4 passes "sol > -4.5" but the lower bound -5.0 does not
    c = mk("x", (-4.4, 1.2), 0.1)
    c["predictions"]["sol"].update(interval_low=-5.0, interval_high=-3.8)
    for use, expect in ((False, True), (True, False)):
        cfg = dict(lam=0.25, weights=[dict(endpoint="sol", weight=1, higher_is_better=True)],
                   limits=[dict(endpoint="sol", min=-4.5, max=None, use_unfavorable_bound=use)])
        assert js([c], cfg, INFOS)[0]["passes"] is expect


def test_matches_python_reference_on_random_cases(js):
    rng = random.Random(11)
    for _ in range(40):
        comps = [mk(i, (rng.uniform(-6.5, -0.5), rng.uniform(0.3, 3.5)), rng.random(), rng.random(),
                    perm=(lambda v, w: (v, v - w / 2, v + w / 2))(rng.uniform(-6.5, -4.2), rng.uniform(0.2, 2.5))) for i in range(rng.randint(2, 10))]
        eps = rng.sample(list(INFOS), rng.randint(1, 4))
        cfg = dict(lam=rng.choice([0, 0.25, 0.5, 1, rng.random()]),
                   weights=[dict(endpoint=e, weight=rng.choice([0, 1, 3, 5, 8, 10]), higher_is_better=rng.random() < 0.5) for e in eps],
                   limits=[dict(endpoint="herg", min=None, max=rng.uniform(0.2, 0.8), use_unfavorable_bound=False),
                           dict(endpoint="sol", min=rng.uniform(-5.5, -3), max=None, use_unfavorable_bound=rng.random() < 0.5)])
        if sum(w["weight"] for w in cfg["weights"]) <= 0:
            cfg["weights"][0]["weight"] = 4
        ref = py_rank(comps, cfg, INFOS)
        got = js(comps, cfg, INFOS)
        for a, b in zip(ref, got):
            assert b["id"] == a["id"]
            for k in ("S", "C", "R"):
                assert b[k] == pytest.approx(a[k], abs=1e-12)
            assert b["passes"] == a["passes"] and b["blendedRank"] == a["blendedRank"] and b["propertyOnlyRank"] == a["propertyOnlyRank"]
