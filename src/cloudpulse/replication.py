"""Replication: re-run candidate experiments to check that verdicts are stable.

A single experiment's confidence interval only describes noise *within* that
run. The A/A calibration showed that noise *between* runs is larger (F-5). So
each key candidate is re-run several times, each time with fresh twin databases
and a different shuffle seed, and the report states how often each verdict
came out the same way.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from cloudpulse import pipeline
from cloudpulse.bench import env as envmod
from cloudpulse.bench.harness import run_experiment
from cloudpulse.bench.spec import Protocol
from cloudpulse.config import REPO_ROOT, Settings
from cloudpulse.meta import store
from cloudpulse.workload import guardrail, packs


def lab_state_for(scenario: str, scale: str = "default", seed: int = 42) -> pipeline.LabState:
    p, sc = packs.scenario(scenario)
    return pipeline.LabState(scale, seed, p.baseline_sql + sc.fault_sql,
                             {**p.baseline_db_settings, **sc.fault_db_settings}, p.name)


def tested_candidates(meta_dsn: str) -> list[dict]:
    """Latest tested candidate per (scenario, description) in the evidence database."""
    rows = store.fetch_all(
        meta_dsn,
        "SELECT DISTINCT ON (i.scenario, c.description) c.*, q.template_name, i.scenario, "
        "v.label AS original_label, v.target->'effect'->>'ratio' AS original_ratio "
        "FROM candidates c JOIN performance_incidents i ON i.id = c.incident_id "
        "JOIN queries q ON q.id = i.query_id JOIN experiments e ON e.candidate_id = c.id "
        "JOIN verdicts v ON v.experiment_id = e.id "
        "ORDER BY i.scenario, c.description, c.id DESC")
    return rows


def replicate(settings: Settings, repeats: int = 3, log=print) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = REPO_ROOT / "results" / "replication" / stamp
    out.mkdir(parents=True)
    report = []
    for cand in tested_candidates(settings.meta_dsn):
        lab_state = lab_state_for(cand["scenario"])
        reps = []
        for r in range(repeats):
            proto = Protocol(blocks=15, warmup_rounds=1, seed=9000 + 17 * r)
            spec, target, guard = pipeline.build_candidate_spec(
                cand, lab_state, proto, f"rep{r}_cand{cand['id']}")
            res = run_experiment(spec, settings, out_root=out / "runs", log=log)
            rep = guardrail.evaluate(res.rows, target, guard)
            eff = rep.target["effect"]
            reps.append({
                "repeat": r, "label": rep.label, "ratio": eff["ratio"],
                "ci": [eff["ci_low"], eff["ci_high"]], "collateral": rep.collateral,
                "warnings": rep.warnings,
                "guardrail": [{k: g[k] for k in ("query", "ratio", "ci_low", "ci_high",
                                                  "collateral_regression")}
                              for g in rep.guardrail],
                "run_dir": str(res.run_dir.relative_to(REPO_ROOT)),
            })
            log(f"[replicate] {cand['scenario']} {cand['description']} rep{r}: {rep.label}")
        labels = Counter(x["label"] for x in reps)
        report.append({
            "scenario": cand["scenario"], "candidate": cand["description"],
            "target_query": cand["template_name"], "original_label": cand["original_label"],
            "original_ratio": float(cand["original_ratio"]), "replications": reps,
            "labels": dict(labels),
            "agrees_with_original": labels.get(cand["original_label"], 0),
        })
        (out / "summary.json").write_text(json.dumps(report, indent=2, default=str))
    for c in report:
        c["stability"] = consolidate(c)
    summary = {"environment": envmod.capture(settings.lab_dsn, settings.env_label),
               "repeats": repeats, "candidates": report}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "summary.md").write_text(render(summary))
    return out


def consolidate(c: dict) -> dict:
    """Stability verdict over the original run plus its replications (D-023).

    A verdict that flips between runs is reported as UNSTABLE rather than picking
    a side: the effect sits near a decision margin, and a person should decide."""
    labels = [c["original_label"]] + [r["label"] for r in c["replications"]]
    counts = Counter(labels)
    stable = len(counts) == 1
    return {"runs": len(labels), "counts": dict(counts), "stable": stable,
            "consolidated": labels[0] if stable else "UNSTABLE"}


def finalize(out_dir: Path) -> dict:
    """Add the consolidated verdicts to a finished replication and re-render it."""
    summary = json.loads((out_dir / "summary.json").read_text())
    for c in summary["candidates"]:
        c["stability"] = consolidate(c)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out_dir / "summary.md").write_text(render(summary))
    return summary


def render(summary: dict) -> str:
    env = summary["environment"]
    lines = [
        "# Replication of candidate verdicts", "",
        f"- Environment: **{env['env_label']}** | PostgreSQL {env['postgres'].get('server_version')} "
        f"| git {env['git_commit']}",
        f"- Each candidate re-run {summary['repeats']} times with fresh twins and new shuffle seeds.", "",
        "| Scenario | Candidate | Original verdict | Replications | Consolidated | "
        "Target time ratio per run | Collateral per run |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in summary["candidates"]:
        reps = c["replications"]
        labels = ", ".join(f"{k} ×{v}" for k, v in c["labels"].items())
        ratios = ", ".join(f"{x['ratio']:.3f}" for x in reps)
        coll = "; ".join(
            ", ".join(f"{g['query']} {g['ratio']:.2f}×" for g in x["guardrail"] if g["collateral_regression"])
            or "none" for x in reps)
        stab = c.get("stability") or consolidate(c)
        lines.append(f"| {c['scenario']} | `{c['candidate']}` | {c['original_label']} "
                     f"({c['original_ratio']:.3f}) | {labels} | **{stab['consolidated']}** | "
                     f"{ratios} | {coll} |")
    return "\n".join(lines) + "\n"
