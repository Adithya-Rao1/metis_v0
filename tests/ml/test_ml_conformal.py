import numpy as np
import pytest

from ml.uncertainty.conformal import (N_VALUES, ConformalByN, IsotonicRecalibrator, SplitConformal, VennAbers,
                                      coverage_report, ece, reliability_data)


def synth(n, M=20, seed=0):
    rng = np.random.default_rng(seed)
    sim = rng.uniform(0.05, 1.0, n)
    scale = 0.2 + 1.5 * (1 - sim)
    truth = rng.normal(0, 2, n)
    common = rng.normal(0, 1, n) * scale
    preds = truth + common + rng.normal(0, 1, (M, n)) * 0.3 * scale
    return preds, truth, sim


@pytest.mark.parametrize("N", N_VALUES)
def test_coverage_near_target_at_every_N(N):
    Pc, yc, sc = synth(1500, seed=1)
    Pt, yt, st = synth(4000, seed=2)
    cb = ConformalByN.fit(Pc, yc, sc, alpha=0.1)
    _, lo, hi = cb.predict(Pt, st, N)
    rep = coverage_report(yt, lo, hi, st)
    assert abs(rep["coverage"] - 0.9) < 0.03, rep["coverage"]
    for b in rep["by_similarity_bin"]:
        if b["n"] > 200:
            assert abs(b["coverage"] - 0.9) < 0.08, b


def test_calibration_is_per_N():
    Pc, yc, sc = synth(1500, seed=1)
    cb = ConformalByN.fit(Pc, yc, sc)
    assert set(cb.by_n) == set(N_VALUES)
    assert len({round(c.q, 6) for c in cb.by_n.values()}) > 1  # distinct calibrations, not one shared q
    with pytest.raises(ValueError):
        ConformalByN.fit(Pc[:5], yc, sc)  # N=10 needs 10 members


def test_scaled_intervals_narrower_in_distribution():
    Pc, yc, sc = synth(1500, seed=1)
    Pt, yt, st = synth(3000, seed=2)
    cb = ConformalByN.fit(Pc, yc, sc)
    _, lo, hi = cb.predict(Pt, st, 5)
    w = hi - lo
    assert w[st > 0.8].mean() < 0.5 * w[st < 0.3].mean()
    flat = SplitConformal.fit(Pc[:5].mean(0), yc, sc, scaled=False)
    l2, h2 = flat.interval(Pt[:5].mean(0), st)
    assert np.ptp(h2 - l2) < 1e-9  # unscaled is constant width


def test_scaled_beats_unscaled_on_binwise_coverage():
    Pc, yc, sc = synth(1500, seed=1)
    Pt, yt, st = synth(4000, seed=2)
    flat = SplitConformal.fit(Pc[:5].mean(0), yc, sc, scaled=False)
    lo, hi = flat.interval(Pt[:5].mean(0), st)
    ood = coverage_report(yt, lo, hi, st)["by_similarity_bin"][0]["coverage"]
    cb = ConformalByN.fit(Pc, yc, sc)
    _, lo, hi = cb.predict(Pt, st, 5)
    ood_scaled = coverage_report(yt, lo, hi, st)["by_similarity_bin"][0]["coverage"]
    assert abs(ood_scaled - 0.9) < abs(ood - 0.9)


def test_serialisation_roundtrip():
    Pc, yc, sc = synth(600, seed=1)
    cb = ConformalByN.fit(Pc, yc, sc)
    cb2 = ConformalByN.from_dict(cb.to_dict())
    a, b = cb.predict(Pc, sc, 10), cb2.predict(Pc, sc, 10)
    assert all(np.allclose(x, y) for x, y in zip(a, b))


def synth_cls(n, seed):
    rng = np.random.default_rng(seed)
    z = rng.normal(0, 1.5, n)
    y = (rng.random(n) < 1 / (1 + np.exp(-z))).astype(int)
    s = 1 / (1 + np.exp(-3 * z))  # overconfident scores
    return s, y


def test_ece_improves_after_recalibration():
    sc, yc = synth_cls(1500, 1)
    st, yt = synth_cls(3000, 2)
    raw = ece(st, yt)
    iso = ece(IsotonicRecalibrator().fit(sc, yc).predict(st), yt)
    va_p = VennAbers().fit(sc, yc).predict(st[:600])
    va = ece(va_p, yt[:600])
    assert iso < raw * 0.5 and va < ece(st[:600], yt[:600]) * 0.5, (raw, iso, va)
    p0, p1 = VennAbers().fit(sc, yc).predict_interval(st[:20])
    assert np.all(p0 <= p1 + 1e-12) and np.all((va_p >= 0) & (va_p <= 1))
    rd = reliability_data(st, yt)
    assert rd["n"] == 3000 and sum(b["count"] for b in rd["bins"]) == 3000 and abs(rd["ece"] - raw) < 1e-12
