"""Statistical verifier: unit tests plus simulation checks of the method itself."""

import math

import numpy as np
import pytest

from cloudpulse.stats import verifier as v


def synth_rows(n_blocks, n_params, true_ratio, noise=0.15, seed=0, query="q", block_noise=0.1):
    """Simulated paired experiment: lognormal per-execution noise plus a
    shared per-block drift (both arms in a block see the same drift)."""
    rng = np.random.default_rng(seed)
    base = rng.lognormal(mean=math.log(1000), sigma=1.0, size=n_params)  # param costs (us)
    rows = []
    for b in range(n_blocks):
        drift = math.exp(rng.normal(0, block_noise))
        for arm, factor in (("A", 1.0), ("B", true_ratio)):
            for p in range(n_params):
                lat = base[p] * factor * drift * math.exp(rng.normal(0, noise))
                rows.append({"query": query, "block": b, "arm": arm, "status": "ok",
                             "latency_us": lat})
    return rows


def test_point_estimate_recovers_known_ratio():
    eff = v.estimate(synth_rows(30, 20, 0.5, seed=1), "q")
    assert 0.45 < eff.ratio < 0.55
    assert eff.ci_low < 0.5 < eff.ci_high


def test_block_drift_is_cancelled_by_pairing():
    # Large drift between blocks, identical arms: estimate must stay near 1.
    eff = v.estimate(synth_rows(20, 20, 1.0, block_noise=0.8, seed=2), "q")
    assert abs(eff.ratio - 1) < 0.05


def test_ci_coverage_by_simulation():
    """Percentile block bootstrap with 20 blocks should cover the true ratio
    roughly 95% of the time. Bootstrap intervals are known to under-cover
    slightly with few blocks, so we accept >= 88%."""
    true = 0.8
    hits = 0
    sims = 200
    for s in range(sims):
        eff = v.estimate(synth_rows(20, 10, true, noise=0.3, seed=100 + s), "q", n_boot=2000, seed=s)
        hits += eff.ci_low <= true <= eff.ci_high
    assert hits / sims >= 0.88


def test_aa_false_supported_rate_is_low():
    falses = 0
    for s in range(200):
        eff = v.estimate(synth_rows(15, 10, 1.0, noise=0.3, seed=500 + s), "q", n_boot=2000, seed=s)
        falses += v.decide_target(eff).label == v.SUPPORTED
    assert falses / 200 <= 0.03


@pytest.mark.parametrize("ratio,label", [(0.5, v.SUPPORTED), (1.0, v.REJECTED), (1.5, v.REJECTED)])
def test_decision_rule(ratio, label):
    eff = v.estimate(synth_rows(20, 20, ratio, noise=0.05, seed=3), "q")
    assert v.decide_target(eff).label == label


def test_straddling_threshold_is_inconclusive():
    eff = v.Effect("q", 20, 0.95, 0.90, 1.0, 0.95, 1, 1)
    assert v.decide_target(eff).label == v.INCONCLUSIVE


def test_too_few_blocks_is_inconclusive():
    eff = v.estimate(synth_rows(5, 10, 0.5, seed=4), "q")
    assert v.decide_target(eff, min_blocks=10).label == v.INCONCLUSIVE


def test_candidate_timeouts_reject():
    rows = synth_rows(12, 5, 0.5, seed=5)
    rows.append({"query": "q", "block": 0, "arm": "B", "status": "timeout", "latency_us": float("nan")})
    assert v.decide_target(v.estimate(rows, "q")).label == v.REJECTED


def test_collateral_regression_detection():
    worse = v.estimate(synth_rows(20, 20, 1.5, noise=0.05, seed=6), "q", confidence=0.99)
    same = v.estimate(synth_rows(20, 20, 1.02, noise=0.05, seed=7), "q", confidence=0.99)
    assert v.is_collateral_regression(worse)
    assert not v.is_collateral_regression(same)
