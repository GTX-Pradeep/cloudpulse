import numpy as np

from cloudpulse.telemetry.detector import DetectorConfig, detect


def series(values, calls=10, invalid=()):
    return [{"mean_ms": v, "calls": calls, "invalid": i in invalid} for i, v in enumerate(values)]


def noisy(n, level=10.0, cv=0.1, seed=0):
    rng = np.random.default_rng(seed)
    return list(level * np.exp(rng.normal(0, cv, n)))


def test_insufficient_history_is_reported_not_ok():
    assert detect(series([10, 11, 10])).state == "insufficient_data"


def test_stable_noisy_series_is_not_flagged():
    flagged = sum(detect(series(noisy(60, seed=s))).state == "regression" for s in range(100))
    assert flagged <= 2


def test_sustained_step_is_detected_with_expected_delay():
    vals = noisy(20, seed=1) + noisy(6, level=40, seed=2)
    d = detect(series(vals))
    assert d.state == "regression"
    assert d.index == 22  # third anomalous interval after the step at 20
    assert 3.0 < d.ratio < 5.5


def test_single_spike_is_not_a_regression():
    vals = noisy(20, seed=3)
    vals[15] = 500
    assert detect(series(vals)).state != "regression"


def test_small_but_statistically_unusual_change_is_ignored():
    # 30% slower is below min_ratio=1.5: not a practical regression
    vals = noisy(20, cv=0.01, seed=4) + noisy(6, level=13, cv=0.01, seed=5)
    assert detect(series(vals)).state != "regression"


def test_flat_baseline_does_not_explode():
    # MAD = 0; the relative floor prevents infinite z-scores on tiny wobbles
    vals = [10.0] * 20 + [10.2] * 6
    assert detect(series(vals)).state != "regression"


def test_invalid_intervals_are_skipped():
    vals = noisy(20, seed=6) + [1e6] + noisy(5, seed=7)
    d = detect(series(vals, invalid={20}))
    assert d.state != "regression"


def test_intervals_without_calls_are_skipped():
    s = series(noisy(20, seed=8))
    s.append({"mean_ms": None, "calls": 0, "invalid": False})
    assert detect(s).state == "ok"


def test_config_is_respected():
    vals = noisy(20, seed=9) + noisy(2, level=40, seed=10)
    assert detect(series(vals)).state != "regression"
    assert detect(series(vals), DetectorConfig(sustain_k=2, sustain_n=2)).state == "regression"
