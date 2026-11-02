"""A/A calibration: run experiments where both arms are identical.

If the method is sound, identical arms should:
  * almost never be declared SUPPORTED (that would be a false "improvement"),
  * almost never be flagged as a collateral regression,
  * produce confidence intervals that contain 1.0 about 95% of the time.

Each repeat rebuilds the twin databases, so the check also covers any
difference introduced by cloning. Results are written to results/calibration/.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from cloudpulse.bench import env as envmod
from cloudpulse.bench.harness import run_experiment
from cloudpulse.bench.spec import ArmSpec, ExperimentSpec, Protocol, QuerySpec
from cloudpulse.config import REPO_ROOT, Settings
from cloudpulse.stats import verifier
from cloudpulse.workload.suite import BASELINE_DB_SETTINGS, BASELINE_SQL, BY_NAME

AA_QUERIES = ["orders_by_customer", "order_by_id", "recent_orders_feed",
              "segment_revenue_for_status", "touch_order"]


def _q(name: str) -> QuerySpec:
    q = BY_NAME[name]
    p = dict(q.params)
    if "n" in p:
        p["n"] = min(p["n"], 20)
    return QuerySpec(q.name, q.sql, q.kind, p, q.weight, q.compare_results)


def run_calibration(settings: Settings, repeats: int = 10, blocks: int = 12,
                    scale: str = "default", seed: int = 42, log=print) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = REPO_ROOT / "results" / "calibration" / stamp
    out.mkdir(parents=True)
    records = []
    for r in range(repeats):
        spec = ExperimentSpec(
            name=f"aa_rep{r}", description="A/A calibration: identical arms",
            scale=scale, seed=seed, baseline_sql=BASELINE_SQL,
            queries=[_q(n) for n in AA_QUERIES],
            arm_a=ArmSpec("A", [], dict(BASELINE_DB_SETTINGS)),
            arm_b=ArmSpec("B", [], dict(BASELINE_DB_SETTINGS)),
            protocol=Protocol(blocks=blocks, warmup_rounds=1, seed=1000 + r),
        )
        res = run_experiment(spec, settings, out_root=out / "runs", log=log)
        for name in AA_QUERIES:
            eff = verifier.estimate(res.rows, name, confidence=0.95, seed=r)
            ver = verifier.decide_target(eff)
            eff_adj = verifier.estimate(res.rows, name, confidence=1 - 0.05 / 4, seed=r)
            records.append({
                "repeat": r, "query": name, "ratio": eff.ratio,
                "ci_low": eff.ci_low, "ci_high": eff.ci_high,
                "ci_contains_1": eff.ci_low <= 1.0 <= eff.ci_high,
                "verdict": ver.label,
                "false_collateral_flag": verifier.is_collateral_regression(eff_adj),
            })
    n = len(records)
    summary = {
        "environment": envmod.capture(settings.lab_dsn, settings.env_label),
        "repeats": repeats, "blocks": blocks, "queries": AA_QUERIES, "n_estimates": n,
        "false_supported": sum(x["verdict"] == "SUPPORTED" for x in records),
        "false_collateral": sum(x["false_collateral_flag"] for x in records),
        "ci_contains_1": sum(x["ci_contains_1"] for x in records),
        "verdict_counts": {k: sum(x["verdict"] == k for x in records)
                           for k in ("SUPPORTED", "REJECTED", "INCONCLUSIVE")},
        "max_abs_change_pct": round(max(abs(x["ratio"] - 1) for x in records) * 100, 2),
        "records": records,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    log(json.dumps({k: v for k, v in summary.items() if k not in ("records", "environment")}, indent=2))
    return out
