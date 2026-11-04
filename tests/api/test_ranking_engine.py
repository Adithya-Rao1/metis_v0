"""Ranking engine: hand-computed cases and property tests (spec section 7)."""

import math
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.ranking.engine import (
    EndpointInfo,
    RankingError,
    binary_entropy,
    confidence,
    desirability,
    rank_compounds,
)
from backend.schemas.contracts import (
    HardLimit,
    Prediction,
    PropertyWeight,
    RankingConfig,
    UncertaintyBreakdown,
)

MV = uuid.uuid4()
BD = UncertaintyBreakdown(ensemble_disagreement=0.5, similarity_to_training=0.3, assay_noise=0.2)


def reg(ep, v, w=0.5):
    return Prediction(endpoint=ep, kind="regression", model_version_id=MV, ensemble_n=5, value=v, interval_low=v - w, interval_high=v + w,
                      coverage=0.9, sample_size=10, method_note="t", breakdown=BD)


def cls(ep, p):
    return Prediction(endpoint=ep, kind="classification", model_version_id=MV, ensemble_n=5, value=p, probability=p, coverage=0.9,
                      sample_size=10, method_note="t", breakdown=BD)


INFO = {"a": EndpointInfo((0.0, 10.0), median_width=1.0), "b": EndpointInfo((0.0, 1.0)), "c": EndpointInfo((-5.0, 5.0), median_width=2.0)}


def test_desirability_hand_cases():
    assert desirability(5, 0, 10, True) == 0.5
    assert desirability(2, 0, 10, False) == pytest.approx(0.8)
    assert desirability(-3, 0, 10, True) == 0.0 and desirability(99, 0, 10, True) == 1.0
    assert desirability(-3, 0, 10, False) == 1.0 and desirability(99, 0, 10, False) == 0.0


def test_confidence_hand_cases():
    assert confidence(reg("a", 5, 0.25), 1.0) == pytest.approx(0.75)  # width 0.5 -> 1 - 0.5/2
    assert confidence(reg("a", 5, 1.0), 1.0) == 0.0  # width 2 -> 1 - 2/2
    assert confidence(reg("a", 5, 5.0), 1.0) == 0.0  # floored
    assert confidence(cls("b", 0.5), None) == pytest.approx(0.0)
    assert confidence(cls("b", 1.0), None) == pytest.approx(1.0)
    assert confidence(cls("b", 0.25), None) == pytest.approx(1 - binary_entropy(0.25))
    assert binary_entropy(0.25) == pytest.approx(0.8112781244591328)
    with pytest.raises(RankingError):
        confidence(reg("a", 5), None)


def test_blended_hand_computed():
    # weights a:3 (higher better), b:1 (lower better); lam 0.25
    cfg = RankingConfig(weights=[PropertyWeight(endpoint="a", weight=3, higher_is_better=True), PropertyWeight(endpoint="b", weight=1, higher_is_better=False)], lam=0.25)
    preds = {"X": {"a": reg("a", 8, 0.25), "b": cls("b", 0.25)}, "Y": {"a": reg("a", 6, 0.5), "b": cls("b", 0.0)}}
    out = {r.compound_id: r for r in rank_compounds(preds, cfg, INFO)}
    # X: d_a=.8, d_b=1-.25=.75 -> S=(3*.8+.75)/4=.7875 ; c_a=.75, c_b=1-H(.25)=.18872 -> C=(3*.75+.18872)/4
    cx = (3 * 0.75 + (1 - 0.8112781244591328)) / 4
    assert out["X"].property_score == pytest.approx(0.7875)
    assert out["X"].confidence_score == pytest.approx(cx)
    assert out["X"].blended_score == pytest.approx(0.75 * 0.7875 + 0.25 * cx)
    # Y: d_a=.6, d_b=1 -> S=(1.8+1)/4=.7 ; c_a=0 (width 1 -> 1-1/2=.5!) recompute: width=1.0 -> c=.5 ; c_b=1 -> C=(1.5+1)/4=.625
    assert out["Y"].property_score == pytest.approx(0.7)
    assert out["Y"].confidence_score == pytest.approx(0.625)
    assert out["Y"].blended_score == pytest.approx(0.75 * 0.7 + 0.25 * 0.625)
    assert out["X"].blended_rank == 1 and out["Y"].blended_rank == 2
    # stacked breakdown sums to R
    for r in out.values():
        assert sum(p.blended_property_part + p.blended_confidence_part for p in r.breakdown) == pytest.approx(r.blended_score)
        assert sum(p.property_contribution for p in r.breakdown) == pytest.approx(r.property_score)


def test_user_range_overrides_default():
    cfg = RankingConfig(weights=[PropertyWeight(endpoint="a", weight=1, higher_is_better=True, target_min=4, target_max=6)], lam=0)
    r = rank_compounds({"X": {"a": reg("a", 5)}}, cfg, INFO)[0]
    assert r.property_score == pytest.approx(0.5)


def test_lam_extremes_and_rank_divergence():
    cfg0 = RankingConfig(weights=[PropertyWeight(endpoint="a", weight=1, higher_is_better=True)], lam=0)
    cfg1 = cfg0.model_copy(update={"lam": 1.0})
    preds = {"hi_wide": {"a": reg("a", 9, 1.9)}, "lo_tight": {"a": reg("a", 6, 0.1)}}
    r0 = {r.compound_id: r for r in rank_compounds(preds, cfg0, INFO)}
    r1 = {r.compound_id: r for r in rank_compounds(preds, cfg1, INFO)}
    assert r0["hi_wide"].blended_rank == 1 and r1["lo_tight"].blended_rank == 1
    assert r1["hi_wide"].property_rank == 1  # property-only rank is unchanged by lam


def test_hard_limits_point_vs_unfavorable_and_fail_ranking():
    w = [PropertyWeight(endpoint="a", weight=1, higher_is_better=True)]
    preds = {"best_fails": {"a": reg("a", 9.0, 2.0)}, "ok": {"a": reg("a", 3.0, 0.1)}}
    point = RankingConfig(weights=w, limits=[HardLimit(endpoint="a", min=8.0)], lam=0)
    out = {r.compound_id: r for r in rank_compounds(preds, point, INFO)}
    assert out["best_fails"].passes_limits and out["ok"].limit_failures[0].note.startswith("limit: >= 8")
    assert out["best_fails"].blended_rank == 1 and out["ok"].blended_rank == 2
    unfav = RankingConfig(weights=w, limits=[HardLimit(endpoint="a", min=8.0, use_unfavorable_bound=True)], lam=0)
    out = {r.compound_id: r for r in rank_compounds(preds, unfav, INFO)}
    assert not out["best_fails"].passes_limits and "unfavorable" in out["best_fails"].limit_failures[0].note
    # failer is kept and ranks below passers: here both fail (ok fails min 8 too); a passer ranks first
    lim = RankingConfig(weights=w, limits=[HardLimit(endpoint="a", max=5.0)], lam=0)
    out = {r.compound_id: r for r in rank_compounds(preds, lim, INFO)}
    assert out["ok"].blended_rank == 1 and out["best_fails"].blended_rank == 2 and not out["best_fails"].passes_limits
    assert out["best_fails"].property_rank == 2


def test_limit_on_unweighted_endpoint_and_missing_data():
    cfg = RankingConfig(weights=[PropertyWeight(endpoint="a", weight=1, higher_is_better=True)], limits=[HardLimit(endpoint="b", max=0.3)], lam=0.25)
    out = rank_compounds({"X": {"a": reg("a", 5), "b": cls("b", 0.9)}}, cfg, INFO)[0]
    assert not out.passes_limits
    with pytest.raises(RankingError):
        rank_compounds({"X": {"a": reg("a", 5)}}, cfg, INFO)


def test_ties_share_rank():
    cfg = RankingConfig(weights=[PropertyWeight(endpoint="a", weight=1, higher_is_better=True)], lam=0)
    out = rank_compounds({"x": {"a": reg("a", 5)}, "y": {"a": reg("a", 5)}, "z": {"a": reg("a", 1)}}, cfg, INFO)
    assert [r.blended_rank for r in out] == [1, 1, 3]


def test_no_verdict_fields():
    cfg = RankingConfig(weights=[PropertyWeight(endpoint="a", weight=1, higher_is_better=True)])
    r = rank_compounds({"x": {"a": reg("a", 5)}}, cfg, INFO)[0]
    assert not {"verdict", "pass", "fail", "status", "color"} & set(r.model_dump())


val = st.floats(-20, 20, allow_nan=False)


@st.composite
def scenario(draw):
    n_ep = draw(st.integers(1, 3))
    eps = ["a", "c", "b"][:n_ep]
    weights = [PropertyWeight(endpoint=e, weight=draw(st.one_of(st.just(0.0), st.floats(1e-3, 10))), higher_is_better=draw(st.booleans())) for e in eps]
    if sum(w.weight for w in weights) <= 0:
        weights[0] = weights[0].model_copy(update={"weight": 1.0})
    lam = draw(st.floats(0, 1))
    comps = {}
    for i in range(draw(st.integers(1, 6))):
        d = {}
        for e in eps:
            d[e] = cls(e, draw(st.floats(0, 1))) if e == "b" else reg(e, draw(val), draw(st.floats(0, 8)))
        comps[f"c{i}"] = d
    return RankingConfig(weights=weights, lam=lam), comps


@settings(max_examples=80, deadline=None)
@given(scenario())
def test_property_scores_bounded_and_blend_identity(sc):
    cfg, comps = sc
    out = rank_compounds(comps, cfg, INFO)
    for r in out:
        assert -1e-9 <= r.property_score <= 1 + 1e-9 and -1e-9 <= r.confidence_score <= 1 + 1e-9
        assert math.isclose(r.blended_score, (1 - cfg.lam) * r.property_score + cfg.lam * r.confidence_score, abs_tol=1e-12)
        assert sum(p.blended_property_part + p.blended_confidence_part for p in r.breakdown) == pytest.approx(r.blended_score, abs=1e-9)
    assert sorted(r.blended_rank for r in out)[0] == 1


@settings(max_examples=60, deadline=None)
@given(scenario())
def test_property_lam_zero_ranks_equal_property_ranks(sc):
    cfg, comps = sc
    out = rank_compounds(comps, cfg.model_copy(update={"lam": 0.0}), INFO)
    assert [r.blended_rank for r in out] == [r.property_rank for r in out]


@settings(max_examples=60, deadline=None)
@given(scenario(), st.floats(0.1, 100))
def test_property_weight_scale_invariant(sc, k):
    cfg, comps = sc
    scaled = cfg.model_copy(update={"weights": [w.model_copy(update={"weight": w.weight * k}) for w in cfg.weights]})
    a, b = rank_compounds(comps, cfg, INFO), rank_compounds(comps, scaled, INFO)
    for x, y in zip(a, b):
        assert x.blended_score == pytest.approx(y.blended_score, abs=1e-9)


@settings(max_examples=60, deadline=None)
@given(scenario(), st.floats(0, 12))
def test_property_failers_rank_below_passers(sc, thr):
    cfg, comps = sc
    cfg = cfg.model_copy(update={"limits": [HardLimit(endpoint=cfg.weights[0].endpoint, max=thr - 10)]})
    out = rank_compounds(comps, cfg, INFO)
    passers = [r for r in out if r.passes_limits]
    failers = [r for r in out if not r.passes_limits]
    for f in failers:
        for p in passers:
            assert p.blended_rank < f.blended_rank and p.property_rank < f.property_rank


@settings(max_examples=60, deadline=None)
@given(st.floats(-20, 20), st.floats(0, 5), st.booleans())
def test_property_desirability_monotonic(v, delta, hib):
    d1, d2 = desirability(v, 0, 10, hib), desirability(v + delta, 0, 10, hib)
    assert 0 <= d1 <= 1 and 0 <= d2 <= 1
    assert d2 >= d1 - 1e-12 if hib else d2 <= d1 + 1e-12
