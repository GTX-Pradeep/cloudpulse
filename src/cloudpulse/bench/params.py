"""Deterministic parameter samplers.

A query template like `WHERE customer_id = %s` has no single latency: a customer
with 40,000 orders and one with 3 orders behave very differently. Benchmarks
therefore run each template over a fixed, seeded *mix* of parameter values, and
both arms see exactly the same values in the same order within a block.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from cloudpulse import db
from cloudpulse.datagen.generate import EPOCH_END

Params = list[tuple]


def _end() -> datetime:
    return datetime.fromisoformat(EPOCH_END.replace("+00", "+00:00")).astimezone(UTC)


def customer_mix(dsn: str, n: int, seed: int) -> Params:
    """Stratified customers: 20% from the top 1% by order count, then 40% from
    the rest of the top half, 40% from the bottom half (customers with >=1 order)."""
    with db.cursor(dsn) as cur:
        cur.execute(
            "SELECT customer_id FROM orders GROUP BY customer_id "
            "ORDER BY count(*) DESC, customer_id"
        )
        ids = np.array([r[0] for r in cur.fetchall()])
    rng = np.random.default_rng(seed)
    top = ids[: max(1, len(ids) // 100)]
    mid = ids[len(top) : len(ids) // 2]
    low = ids[len(ids) // 2 :]
    n_top = max(1, round(n * 0.2))
    n_mid = round(n * 0.4)
    n_low = n - n_top - n_mid
    pick = np.concatenate(
        [
            rng.choice(top, n_top, replace=len(top) < n_top),
            rng.choice(mid, n_mid, replace=False),
            rng.choice(low, n_low, replace=False),
        ]
    )
    return [(int(x),) for x in pick]


def heavy_hitters(spec: dict[str, Any], dsn: str, k: int = 20) -> Params:
    """The k heaviest parameter values for a sampler (F-9).

    Plan flips concentrate on the most frequent values, which the planner treats
    specially (most-common-values statistics) and which a random sample rarely
    contains. What-if checks always include them. Empty for samplers without a
    notion of "heavy"."""
    if spec.get("type") != "customer_mix":
        return []
    with db.cursor(dsn) as cur:
        cur.execute(
            "SELECT customer_id FROM orders GROUP BY customer_id "
            "ORDER BY count(*) DESC, customer_id LIMIT %s", (k,)
        )
        return [(int(r[0]),) for r in cur.fetchall()]


def order_ids(dsn: str, n: int, seed: int) -> Params:
    with db.cursor(dsn) as cur:
        cur.execute("SELECT min(id), max(id) FROM orders")
        lo, hi = cur.fetchone()
    rng = np.random.default_rng(seed)
    return [(int(x),) for x in rng.integers(lo, hi + 1, size=n)]


def hours_before_end(dsn: str, n: int, seed: int, max_hours: float = 48.0) -> Params:
    rng = np.random.default_rng(seed)
    end = _end()
    return [(end - timedelta(hours=float(h)),) for h in rng.uniform(1.0, max_hours, size=n)]


def sample(spec: dict[str, Any], dsn: str, seed: int) -> Params:
    kind = spec.get("type", "none")
    n = int(spec.get("n", 1))
    if kind == "none":
        return [()] * n
    if kind == "values":
        return [tuple(v) for v in spec["values"]]
    if kind == "customer_mix":
        return customer_mix(dsn, n, seed)
    if kind == "order_ids":
        return order_ids(dsn, n, seed)
    if kind == "sql":
        # generic, deterministic: setseed() then a query that uses random();
        # used by packs whose parameters are defined by a specification (TPC-H)
        with db.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT setseed(%s)", ((seed % 1_000_003) / 1_000_003.0,))
            cur.execute(spec["query"], {"n": n})
            return [tuple(r) for r in cur.fetchall()]
    if kind == "union":
        # a regular sample plus values that must be covered: explicit ones (e.g.
        # parameters for which HypoPG predicts a plan change) and/or other samplers
        base = sample(spec["base"], dsn, seed)
        extra = [tuple(v) for v in spec.get("extra", [])]
        for other in spec.get("also", []):
            extra += sample(other, dsn, seed)
        out = list(base)
        for p in extra:
            if p not in out:
                out.append(p)
        return out
    if kind == "heavy_hitters":
        return heavy_hitters(spec["of"], dsn, int(spec.get("k", 20)))
    if kind == "hours_before_end":
        return hours_before_end(dsn, n, seed, float(spec.get("max_hours", 48)))
    raise ValueError(f"unknown parameter sampler {kind!r}")
