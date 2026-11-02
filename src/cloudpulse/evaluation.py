"""Ground-truth evaluation: inject known faults and score the whole pipeline.

For each scenario:
  1. recreate the target database from a healthy template (data + baseline indexes)
  2. run the tracked workload for `baseline_rounds` rounds, snapshotting telemetry
  3. inject the fault, run `fault_rounds` more rounds
  4. investigate every incident the detector opened (plans, diagnosis, HypoPG,
     lab experiments with guardrail) through the job queue + worker
  5. score: detected? how many rounds late? right diagnosis? which verdict?

False positives are defined strictly: any regression declared while no fault was
active (every baseline phase, plus the whole s0_control scenario).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cloudpulse import db, pipeline
from cloudpulse.bench import env as envmod
from cloudpulse.config import REPO_ROOT, Settings
from cloudpulse.datagen import generate
from cloudpulse.jobs import queue, worker
from cloudpulse.meta import store
from cloudpulse.telemetry import collector, detector
from cloudpulse.workload.generator import NoisyNeighbor, WorkloadRunner
from cloudpulse.workload.suite import (
    BASELINE_DB_SETTINGS,
    BASELINE_SQL,
    SCENARIOS,
    WORKLOAD,
    Scenario,
)

TEMPLATE_DB = "cp_target_template"


@dataclass
class ScenarioResult:
    scenario: str
    title: str
    rounds: dict
    baseline_false_positives: list[str]
    flagged_after_fault: dict[str, int]  # query -> rounds after fault when declared
    expected_query: str | None
    detected: bool | None
    detection_delay_rounds: int | None
    diagnosis_rules: list[str] = field(default_factory=list)
    diagnosis_correct: bool | None = None
    candidates: list[dict] = field(default_factory=list)
    incidents: list[dict] = field(default_factory=list)
    seconds: float = 0.0


def _admin(target_dsn: str) -> str:
    return db.with_database(target_dsn, "postgres")


def build_target_template(settings: Settings, scale: str, seed: int, log=print) -> None:
    admin = _admin(settings.target_dsn)
    if db.database_exists(admin, TEMPLATE_DB):
        dsn = db.with_database(admin, TEMPLATE_DB)
        with db.cursor(dsn) as cur:
            cur.execute("SELECT value FROM cloudpulse_dataset WHERE key IN ('name','seed') ORDER BY key")
            vals = [r[0] for r in cur.fetchall()]
        if vals == [scale, str(seed)]:
            return
    log(f"building target template (scale={scale}, seed={seed})")
    dsn = generate.build_database(admin, TEMPLATE_DB, generate.SCALES[scale], seed)
    with db.cursor(dsn) as cur:
        for stmt in BASELINE_SQL:
            cur.execute(stmt)


def reset_target(settings: Settings) -> None:
    admin = _admin(settings.target_dsn)
    name = settings.target_dsn.rsplit("/", 1)[-1].split("?")[0]
    db.drop_database(admin, name)
    db.create_database(admin, name, template=TEMPLATE_DB)
    with db.cursor(admin) as cur:
        for k, v in BASELINE_DB_SETTINGS.items():
            cur.execute(f"ALTER DATABASE {db.check_ident(name)} SET {db.check_ident(k)} = '{v}'")
    with db.cursor(settings.target_dsn) as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        cur.execute("ANALYZE")  # template copy keeps stats, this just refreshes counters
        cur.execute("SELECT pg_stat_statements_reset()")


def inject(settings: Settings, sc: Scenario) -> NoisyNeighbor | None:
    name = settings.target_dsn.rsplit("/", 1)[-1].split("?")[0]
    with db.cursor(settings.target_dsn) as cur:
        for stmt in sc.fault_sql:
            cur.execute(stmt)
    with db.cursor(_admin(settings.target_dsn)) as cur:
        for k, v in sc.fault_db_settings.items():
            cur.execute(f"ALTER DATABASE {db.check_ident(name)} SET {db.check_ident(k)} = '{v}'")
    if sc.noisy_neighbor:
        nn = NoisyNeighbor(settings.target_dsn)
        nn.start()
        return nn
    return None


def _flagged(settings: Settings, cfg: detector.DetectorConfig) -> dict[str, int]:
    out = {}
    for name, qpk in pipeline.tracked_query_pks(settings).items():
        det = detector.detect(collector.interval_series(settings.meta_dsn, qpk), cfg)
        if det.state == "regression":
            out[name] = det.index
    return out


def run_scenario(
    settings: Settings, sc: Scenario, scale: str, seed: int,
    baseline_rounds: int = 14, fault_rounds: int = 8, workload_scale: float = 0.5,
    max_investigations: int = 3, log=print,
) -> ScenarioResult:
    t0 = time.time()
    cfg = detector.DetectorConfig()
    log(f"=== {sc.name}: {sc.title}")
    reset_target(settings)
    qids = collector.template_queryids(settings.target_dsn, WORKLOAD)
    runner = WorkloadRunner(settings.target_dsn, WORKLOAD, scale=workload_scale)
    collector.take_snapshot(settings.target_dsn, settings.meta_dsn, qids)
    runner.run_round()
    collector.take_snapshot(settings.target_dsn, settings.meta_dsn, qids)
    pipeline.capture_baseline_plans(settings, pipeline.tracked_query_pks(settings))

    baseline_fp: set[str] = set()
    for _ in range(baseline_rounds):
        runner.run_round()
        collector.take_snapshot(settings.target_dsn, settings.meta_dsn, qids)
        baseline_fp |= set(_flagged(settings, cfg))
    n_intervals_at_fault = baseline_rounds + 1

    nn = inject(settings, sc)
    log(f"[{sc.name}] fault injected")
    try:
        for _ in range(fault_rounds):
            runner.run_round()
            collector.take_snapshot(settings.target_dsn, settings.meta_dsn, qids)
    finally:
        if nn:
            nn.stop()
    flagged = {q: idx - n_intervals_at_fault + 1 for q, idx in _flagged(settings, cfg).items()
               if q not in baseline_fp}

    opened = pipeline.detect_and_open_incidents(settings, cfg, scenario=sc.name)
    # Investigate the expected query first, then the rest, up to a budget.
    inc_rows = store.fetch_all(
        settings.meta_dsn,
        "SELECT i.id, q.template_name FROM performance_incidents i JOIN queries q ON q.id = i.query_id "
        "WHERE i.id = ANY(%s)", (opened,))
    inc_rows.sort(key=lambda r: (r["template_name"] != sc.regressed_query, r["id"]))
    lab_state = pipeline.LabState(scale, seed, BASELINE_SQL + sc.fault_sql,
                                  {**BASELINE_DB_SETTINGS, **sc.fault_db_settings})
    for r in inc_rows[:max_investigations]:
        queue.enqueue(settings.meta_dsn, "investigate",
                      {"incident_id": r["id"], "lab_state": lab_state.to_dict()})
    worker.drain(settings, log=log)

    res = ScenarioResult(
        scenario=sc.name, title=sc.title,
        rounds={"baseline": baseline_rounds, "after_fault": fault_rounds},
        baseline_false_positives=sorted(baseline_fp),
        flagged_after_fault=flagged,
        expected_query=sc.regressed_query,
        detected=(sc.regressed_query in flagged) if sc.regressed_query else None,
        detection_delay_rounds=flagged.get(sc.regressed_query) if sc.regressed_query else None,
    )
    for r in inc_rows:
        inc = store.fetch_one(settings.meta_dsn,
                              "SELECT id, status, outcome, ratio, evidence->'plan_changed_since_baseline' "
                              "AS plan_changed FROM performance_incidents WHERE id=%s", (r["id"],))
        diags = store.fetch_all(settings.meta_dsn,
                                "SELECT rule, hypothesis FROM diagnoses WHERE incident_id=%s", (r["id"],))
        cands = store.fetch_all(
            settings.meta_dsn,
            "SELECT c.id, c.kind, c.description, c.status, c.hypopg, v.label, v.reason, v.target, "
            "v.guardrail FROM candidates c LEFT JOIN experiments e ON e.candidate_id = c.id "
            "LEFT JOIN verdicts v ON v.experiment_id = e.id WHERE c.incident_id = %s ORDER BY c.id",
            (r["id"],))
        res.incidents.append({"incident_id": r["id"], "query": r["template_name"], **inc,
                              "diagnoses": diags, "candidates": cands})
        if r["template_name"] == sc.regressed_query:
            res.diagnosis_rules = [d["rule"] for d in diags]
            res.candidates = cands
            res.diagnosis_correct = bool(sc.expected_diagnosis in res.diagnosis_rules and any(
                (sc.expected_candidate or "") in c["description"] for c in cands))
    if sc.regressed_query and res.detected is False:
        res.diagnosis_correct = False
    res.seconds = round(time.time() - t0, 1)
    return res


def run_all(settings: Settings, names: list[str] | None, scale: str, seed: int,
            out_root: Path | None = None, log=print, **kw) -> Path:
    store.init_schema(settings.meta_dsn, reset=True)
    build_target_template(settings, scale, seed, log=log)
    out_root = out_root or (REPO_ROOT / "results" / "evaluation")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = out_root / stamp
    out.mkdir(parents=True)
    results = []
    for name in names or list(SCENARIOS):
        r = run_scenario(settings, SCENARIOS[name], scale, seed, log=log, **kw)
        results.append(r)
        (out / "scenarios.json").write_text(json.dumps([asdict(x) for x in results], indent=2,
                                                       default=str))
    from cloudpulse.similarity import search
    search.index_all(settings.meta_dsn)  # structural vectors for "similar incidents"
    summary = {
        "environment": envmod.capture(settings.target_dsn, settings.env_label),
        "scale": scale, "seed": seed, "detector": asdict(detector.DetectorConfig()),
        "scenarios": [asdict(x) for x in results],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "summary.md").write_text(render_markdown(summary))
    return out


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{(x - 1) * 100:+.1f}%"


def render_markdown(summary: dict) -> str:
    env = summary["environment"]
    lines = [
        "# CloudPulse ground-truth evaluation",
        "",
        f"- Environment label: **{env['env_label']}**",
        f"- Captured: {env['captured_at_utc']} | git {env['git_commit']}",
        f"- Host: {env['host']['cpu_model']} ({env['host']['cpu_count']} CPUs, "
        f"{env['host']['mem_total_gb']} GB) | PostgreSQL {env['postgres'].get('server_version')}",
        f"- Dataset: scale `{summary['scale']}`, seed {summary['seed']}",
        "",
        "| Scenario | Detected (rounds after fault) | False positives before fault | "
        "Diagnosis | Candidate → verdict |",
        "|---|---|---|---|---|",
    ]
    for s in summary["scenarios"]:
        det = ("—" if s["expected_query"] is None
               else (f"yes ({s['detection_delay_rounds']})" if s["detected"] else "**no**"))
        others = [q for q in s["flagged_after_fault"] if q != s["expected_query"]]
        if others:
            det += f"; also flagged: {', '.join(others)}"
        fp = ", ".join(s["baseline_false_positives"]) or "none"
        diag = ", ".join(s["diagnosis_rules"]) or "—"
        if s["diagnosis_correct"] is not None:
            diag += " ✅" if s["diagnosis_correct"] else " ❌"
        cands = []
        for inc in (s["incidents"] if s["expected_query"] is None else
                    [i for i in s["incidents"] if i["query"] == s["expected_query"]]):
            for c in inc["candidates"]:
                label = c["label"] or c["status"]
                ratio = (c.get("target") or {}).get("effect", {}).get("ratio")
                extra = f" (target {_pct(ratio)})" if ratio else ""
                coll = (c.get("guardrail") or {}).get("collateral") or []
                if coll:
                    extra += f"; collateral: {', '.join(coll)}"
                cands.append(f"`{c['description']}` → **{label}**{extra}")
        lines.append(f"| {s['scenario']} | {det} | {fp} | {diag} | {'<br>'.join(cands) or '—'} |")
    lines += ["", "Raw evidence (plans, every timed execution, verdict reasons) is in the meta "
              "database and in `results/runs/`."]
    return "\n".join(lines) + "\n"
