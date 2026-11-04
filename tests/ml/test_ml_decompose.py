import numpy as np
import pytest

from ml.uncertainty.decompose import decompose


def test_sums_to_one_and_noise_unknown():
    d = decompose([1.0, 1.2, 0.9, 1.1, 1.3], sim_sigma=0.8)
    assert d.assay_noise_known is False and d.assay_noise == 0.0
    assert "unknown" in d.note
    assert abs(d.ensemble_disagreement + d.similarity_to_training + d.assay_noise - 1) < 1e-9
    d.to_breakdown()  # contract validator accepts


def test_known_noise_and_ranges_contain_point():
    d = decompose([1.0, 1.2, 0.9, 1.1, 1.3], sim_sigma=1.0, noise_sd=0.3)
    assert d.assay_noise_known and d.assay_noise > 0
    assert abs(d.ensemble_disagreement + d.similarity_to_training + d.assay_noise - 1) < 1e-9
    for k, (lo, hi) in d.ranges.items():
        v = getattr(d, k)
        assert 0 <= lo <= v <= hi <= 1


def test_deterministic_with_seed():
    rng = np.random.default_rng(0)
    p = rng.normal(0, 1, 20)
    assert decompose(p, 1.5, seed=3).ranges == decompose(p, 1.5, seed=3).ranges
    lo, hi = decompose(p[:3], 1.5).ranges["ensemble_disagreement"]
    assert hi > lo  # N=3 still reports a non-trivial resampling range


def test_distance_share_grows_with_expected_error():
    p = [1.0, 1.1, 0.9, 1.05]
    near, far = decompose(p, 0.1), decompose(p, 3.0)
    assert far.similarity_to_training > near.similarity_to_training


def test_degenerate_and_bad_input():
    assert decompose([1, 1, 1], 0.0).degenerate
    with pytest.raises(ValueError):
        decompose([1.0], 1.0)
