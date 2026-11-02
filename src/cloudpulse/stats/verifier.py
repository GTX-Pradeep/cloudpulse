"""Statistical verification of an A/B benchmark.

Effect measure
--------------
For each block b and query q we have the same parameter list executed on both
arms. The block statistic is the log ratio of total time:

    d_b = log( sum_B(b) / sum_A(b) )

Using the *total* time over the parameter mix weights each parameter by how much
time it actually costs, which is what a workload cares about. Pairing inside a
block cancels slow drift that affects both arms.

The point estimate is ratio = exp(mean(d_b)): 0.25 means arm B takes 25% of A's
time (4x faster); 1.30 means B is 30% slower.

Uncertainty
-----------
Consecutive executions are correlated (shared caches, CPU state), so resampling
individual executions would understate uncertainty. We resample whole blocks
(a paired block bootstrap) and take a percentile interval. Coverage of this
interval is checked by simulation in tests/test_stats.py.

Decision rule (fixed before looking at any result; D-010)
---------------------------------------------------------
Let [L, U] be the CI of the ratio, and `margin` the practical-significance
threshold (default 5%).

  * fewer than `min_blocks` valid blocks, or any timeouts -> INCONCLUSIVE (data problem)
  * U < 1 - margin            -> SUPPORTED  (confidently at least `margin` faster)
  * L > 1 - margin            -> REJECTED   (confidently NOT a meaningful improvement;
                                             includes "confidently slower")
  * otherwise                 -> INCONCLUSIVE (CI straddles the threshold)

For guardrail (collateral) queries the question is different: "is it confidently
worse by more than `harm_margin`?"  -> REGRESSION if L > 1 + harm_margin.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

SUPPORTED = "SUPPORTED"
REJECTED = "REJECTED"
INCONCLUSIVE = "INCONCLUSIVE"


@dataclass
class Effect:
    query: str
    n_blocks: int
    ratio: float  # exp(mean log(B/A)); <1 means B faster
    ci_low: float
    ci_high: float
    confidence: float
    median_us_a: float
    median_us_b: float
    block_ratios: list[float] = field(default_factory=list)
    timeouts_a: int = 0
    timeouts_b: int = 0

    @property
    def change_pct(self) -> float:
        return (self.ratio - 1.0) * 100.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["change_pct"] = round(self.change_pct, 2)
        return d


@dataclass
class Verdict:
    label: str
    reason: str
    effect: Effect

    def to_dict(self) -> dict:
        return {"label": self.label, "reason": self.reason, "effect": self.effect.to_dict()}


def block_log_ratios(rows: list[dict], query: str) -> tuple[np.ndarray, dict]:
    """Per-block log(sum_B / sum_A) for one query, plus descriptive info."""
    sums: dict[tuple[int, str], float] = {}
    lat = {"A": [], "B": []}
    timeouts = {"A": 0, "B": 0}
    for r in rows:
        if r["query"] != query:
            continue
        arm = r["arm"]
        if r["status"] != "ok":
            timeouts[arm] += 1
            continue
        v = float(r["latency_us"])
        lat[arm].append(v)
        sums[(int(r["block"]), arm)] = sums.get((int(r["block"]), arm), 0.0) + v
    blocks = sorted({b for (b, _) in sums})
    d = np.array(
        [math.log(sums[(b, "B")] / sums[(b, "A")]) for b in blocks if (b, "A") in sums and (b, "B") in sums]
    )
    info = {
        "median_us_a": float(np.median(lat["A"])) if lat["A"] else float("nan"),
        "median_us_b": float(np.median(lat["B"])) if lat["B"] else float("nan"),
        "timeouts_a": timeouts["A"],
        "timeouts_b": timeouts["B"],
    }
    return d, info


def bootstrap_ci(d: np.ndarray, confidence: float, n_boot: int, seed: int) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), size=(n_boot, len(d)))
    means = d[idx].mean(axis=1)
    alpha = 1.0 - confidence
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


def estimate(
    rows: list[dict], query: str, confidence: float = 0.95, n_boot: int = 10_000, seed: int = 0
) -> Effect:
    d, info = block_log_ratios(rows, query)
    if len(d) == 0:
        return Effect(query, 0, float("nan"), float("nan"), float("nan"), confidence,
                      info["median_us_a"], info["median_us_b"], [],
                      info["timeouts_a"], info["timeouts_b"])
    lo, hi = bootstrap_ci(d, confidence, n_boot, seed) if len(d) > 1 else (d[0], d[0])
    return Effect(
        query=query,
        n_blocks=len(d),
        ratio=float(math.exp(d.mean())),
        ci_low=float(math.exp(lo)),
        ci_high=float(math.exp(hi)),
        confidence=confidence,
        median_us_a=info["median_us_a"],
        median_us_b=info["median_us_b"],
        block_ratios=[round(float(math.exp(x)), 4) for x in d],
        timeouts_a=info["timeouts_a"],
        timeouts_b=info["timeouts_b"],
    )


def decide_target(effect: Effect, margin: float = 0.05, min_blocks: int = 10) -> Verdict:
    if effect.n_blocks < min_blocks:
        return Verdict(INCONCLUSIVE, f"only {effect.n_blocks} valid blocks (< {min_blocks})", effect)
    if effect.timeouts_b > 0 and effect.timeouts_a == 0:
        return Verdict(REJECTED, f"candidate arm hit {effect.timeouts_b} statement timeouts", effect)
    if effect.timeouts_a or effect.timeouts_b:
        return Verdict(INCONCLUSIVE, "timeouts in both arms; latency is censored", effect)
    thr = 1.0 - margin
    ci = f"{effect.confidence:.0%} CI of time ratio B/A = [{effect.ci_low:.3f}, {effect.ci_high:.3f}]"
    if effect.ci_high < thr:
        return Verdict(SUPPORTED, f"{ci} lies entirely below {thr:.2f} (>= {margin:.0%} faster)", effect)
    if effect.ci_low > thr:
        return Verdict(REJECTED, f"{ci} lies entirely above {thr:.2f}: no meaningful improvement", effect)
    return Verdict(INCONCLUSIVE, f"{ci} straddles the {thr:.2f} threshold", effect)


def is_collateral_regression(effect: Effect, harm_margin: float = 0.10) -> bool:
    if effect.timeouts_b > effect.timeouts_a:
        return True
    return effect.n_blocks > 0 and effect.ci_low > 1.0 + harm_margin
