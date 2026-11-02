"""Regression detector: is this query behaving unusually *for itself*?

A fixed threshold ("slower than 500 ms") is wrong for almost every query: a 2 ms
lookup that becomes 200 ms is a disaster that never crosses it, while a 3 s
report is "slow" every time. So each query is compared with its own history.

Algorithm (per query, on per-interval mean latency):
  1. Skip intervals that are invalid (reset/restart) or have < min_calls calls.
  2. Baseline = the last `window` valid intervals before the current one that
     were not themselves anomalous. Need >= min_baseline of them, otherwise the
     answer is "insufficient data", never "fine".
  3. Robust centre and spread: median and MAD (median absolute deviation). One
     huge outlier barely moves either, unlike mean/stddev.
     scale = max(1.4826 * MAD, rel_floor * median, abs_floor_ms)
     The floors stop a perfectly flat baseline (MAD = 0) from turning every tiny
     wobble into an infinite z-score.
  4. An interval is anomalous if  z = (x - median) / scale > z_threshold
     AND x / median >= min_ratio (statistically unusual AND practically large).
  5. A regression is declared only when >= `sustain_k` of the last `sustain_n`
     valid intervals are anomalous: one slow interval is noise, not an incident.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DetectorConfig:
    window: int = 20
    min_baseline: int = 8
    min_calls: int = 1
    z_threshold: float = 4.0
    min_ratio: float = 1.5
    rel_floor: float = 0.05
    abs_floor_ms: float = 0.05
    sustain_k: int = 3
    sustain_n: int = 4


@dataclass
class Detection:
    state: str  # "insufficient_data" | "ok" | "regression"
    index: int | None = None  # position in the input series where it was declared
    baseline_median: float | None = None
    baseline_mad: float | None = None
    current: float | None = None
    ratio: float | None = None
    z: float | None = None
    anomalous_points: list[int] = field(default_factory=list)


def _mad(values: list[float], med: float) -> float:
    return statistics.median([abs(v - med) for v in values])


def detect(series: list[dict], cfg: DetectorConfig = DetectorConfig()) -> Detection:
    """series: dicts with keys mean_ms, calls, invalid (from query_intervals)."""
    valid: list[tuple[int, float]] = []
    flags: list[bool] = []
    state = Detection("insufficient_data")
    for i, pt in enumerate(series):
        if pt.get("invalid") or pt.get("mean_ms") is None or (pt.get("calls") or 0) < cfg.min_calls:
            continue
        x = float(pt["mean_ms"])
        base = [v for (j, v), f in zip(valid, flags) if not f][-cfg.window:]
        if len(base) < cfg.min_baseline:
            valid.append((i, x))
            flags.append(False)
            continue
        med = statistics.median(base)
        mad = _mad(base, med)
        scale = max(1.4826 * mad, cfg.rel_floor * med, cfg.abs_floor_ms)
        z = (x - med) / scale
        anomalous = z > cfg.z_threshold and x / max(med, 1e-9) >= cfg.min_ratio
        valid.append((i, x))
        flags.append(anomalous)
        recent = flags[-cfg.sustain_n:]
        if anomalous and sum(recent) >= cfg.sustain_k:
            pts = [valid[-len(recent) + k][0] for k, f in enumerate(recent) if f]
            cur = statistics.median([valid[-len(recent) + k][1] for k, f in enumerate(recent) if f])
            return Detection("regression", i, med, mad, cur, cur / med, z, pts)
        state = Detection("ok", None, med, mad, x, x / med if med else None, z)
    return state
