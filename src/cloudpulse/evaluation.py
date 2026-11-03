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
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cloudpulse import db, pipeline
from cloudpulse.bench import env as envmod
from cloudpulse.config import REPO_ROOT, Settings
from cloudpulse.jobs import queue, worker
from cloudpulse.meta import store
from cloudpulse.telemetry import collector, detector
from cloudpulse.workload import packs
from cloudpulse.workload.generator import NoisyNeighbor, WorkloadRunner
from cloudpulse.workload.pack import Pack, Scenario

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
    observed_ratio: dict[str, float] = field(default_factory=dict)
    seconds: float = 0.0


def _admin(target_dsn: str) -> str:
    return db.with_database(target_dsn, "postgres")


def template_db(pack: Pack) -> str:
    return TEMPLATE_DB if pack.name == "ecommerce" else f"{TEMPLATE_DB}_{pack.name}"


def build_target_template(settings: Settings, scale: str, seed: int, pack: Pack, log=print) -> None:
    admin = _admin(settings.target_dsn)
    tdb = template_db(pack)
    if db.database_exists(admin, tdb):
        dsn = db.with_database(admin, tdb)
        with db.cursor(dsn) as cur:
            cur.execute("SELECT value FROM cloudpulse_dataset WHERE key IN ('name','seed') ORDER BY key")
            vals = [r[0] for r in cur.fetchall()]
        if vals == [scale, str(seed)]:
            return
    log(f"building target template (pack={pack.name}, scale={scale}, seed={seed})")
    dsn = pack.build(admin, tdb, scale, seed)
    with db.cursor(dsn) as cur:
        for stmt in pack.baseline_sql:
            cur.execute(stmt)


def reset_target(settings: Settings, pack: Pack) -> None:
    admin = _admin(settings.target_dsn)
    name = settings.target_dsn.rsplit("/", 1)[-1].split("?")[0]
    db.drop_database(admin, name)
    db.create_database(admin, name, template=template_db(pack))
    with db.cursor(admin) as cur:
        cur.execute(f"ALTER DATABASE {db.check_ident(name)} RESET ALL")
        for k, v in pack.baseline_db_settings.items():
            cur.execute(f"ALTER DATABASE {db.check_ident(name)} SET {db.check_ident(k)} = '{v}'")
    with db.cursor(settings.target_dsn) as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        cur.execute("ANALYZE")  # template copy keeps stats, this just refreshes counters
        cur.execute("SELECT pg_stat_statements_reset()")


def inject(settings: Settings, sc: Scenario, pack: Pack) -> NoisyNeighbor | None:
    name = settings.target_dsn.rsplit("/", 1)[-1].split("?")[0]
    with db.cursor(settings.target_dsn) as cur:
        for stmt in sc.fault_sql:
            cur.execute(stmt)
    with db.cursor(_admin(settings.target_dsn)) as cur:
        for k, v in sc.fault_db_settings.items():
            cur.execute(f"ALTER DATABASE {db.check_ident(name)} SET {db.check_ident(k)} = '{v}'")
    if sc.noisy_neighbor:
        nn = NoisyNeighbor(settings.target_dsn, sql=pack.noisy_sql)
        nn.start()
        return nn
    return None


def _flagged(settings: Settings, cfg: detector.DetectorConfig,
             fault_rounds: int | None = None) -> dict[str, int]:
    """Queries currently in regression. With fault_rounds, the value is the
    number of post-fault intervals up to and including the one where the
    regression was declared (1 = the first interval after the fault)."""
    out = {}
    for name, qpk in pipeline.tracked_query_pks(settings).items():
        series = collector.interval_series(settings.meta_dsn, qpk)
        det = detector.detect(series, cfg)
        if det.state == "regression":
            if fault_rounds is None:
                out[name] = det.index
            else:
                # The first round has no earlier snapshot to diff against, so the
                # series has one interval fewer than rounds (F-10). Count from the end.
                out[name] = det.index - (len(series) - fault_rounds) + 1
    return out


def observed_ratios(meta_dsn: str, query_pks: dict[str, int], fault_rounds: int) -> dict[str, float]:
    """Ground-truth size of the fault's effect: median interval mean after the
    fault divided by the median before it, per tracked query. Lets the report
    tell "missed a regression" apart from "nothing regressed"."""
    out = {}
    for name, qpk in query_pks.items():
        vals = [r["mean_ms"] for r in collector.interval_series(meta_dsn, qpk)
                if not r["invalid"] and r["mean_ms"] is not None]
        before, after = vals[:-fault_rounds], vals[-fault_rounds:]
        if len(before) >= 3 and after:
            out[name] = round(statistics.median(after) / statistics.median(before), 3)
    return out


def run_scenario(
    settings: Settings, sc: Scenario, scale: str, seed: int,
    baseline_rounds: int = 14, fault_rounds: int = 8, workload_scale: float = 0.5,
    max_investigations: int = 3, pack: Pack | None = None, log=print,
) -> ScenarioResult:
    pack = pack or packs.get("ecommerce")
    t0 = time.time()
    cfg = detector.DetectorConfig()
    log(f"=== {sc.name}: {sc.title}")
    reset_target(settings, pack)
    qids = collector.template_queryids(settings.target_dsn, pack.workload)
    runner = WorkloadRunner(settings.target_dsn, pack.workload, scale=workload_scale)
    collector.take_snapshot(settings.target_dsn, settings.meta_dsn, qids)
    runner.run_round()
    collector.take_snapshot(settings.target_dsn, settings.meta_dsn, qids)
    pipeline.capture_baseline_plans(settings, pipeline.tracked_query_pks(settings))

    baseline_fp: set[str] = set()
    for _ in range(baseline_rounds):
        runner.run_round()
        collector.take_snapshot(settings.target_dsn, settings.meta_dsn, qids)
        baseline_fp |= set(_flagged(settings, cfg))

    nn = inject(settings, sc, pack)
    log(f"[{sc.name}] fault injected")
    try:
        for _ in range(fault_rounds):
            runner.run_round()
            collector.take_snapshot(settings.target_dsn, settings.meta_dsn, qids)
    finally:
        if nn:
            nn.stop()
    flagged = {q: d for q, d in _flagged(settings, cfg, fault_rounds).items()
               if q not in baseline_fp}

    opened = pipeline.detect_and_open_incidents(settings, cfg, scenario=sc.name)
    # Investigate the expected query first, then the rest, up to a budget.
    inc_rows = store.fetch_all(
        settings.meta_dsn,
        "SELECT i.id, q.template_name FROM performance_incidents i JOIN queries q ON q.id = i.query_id "
        "WHERE i.id = ANY(%s)", (opened,))
    inc_rows.sort(key=lambda r: (r["template_name"] != sc.regressed_query, r["id"]))
    lab_state = pipeline.LabState(scale, seed, pack.baseline_sql + sc.fault_sql,
                                  {**pack.baseline_db_settings, **sc.fault_db_settings}, pack.name)
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
    res.observed_ratio = observed_ratios(settings.meta_dsn, pipeline.tracked_query_pks(settings),
                                         fault_rounds)
    if sc.regressed_query and res.detected is False:
        res.diagnosis_correct = False
    res.seconds = round(time.time() - t0, 1)
    return res


def run_all(settings: Settings, names: list[str] | None, scale: str, seed: int,
            out_root: Path | None = None, log=print, reset_meta: bool = True,
            label: str | None = None, pack: str = "ecommerce", **kw) -> Path:
    p = packs.get(pack)
    store.init_schema(settings.meta_dsn, reset=reset_meta)
    build_target_template(settings, scale, seed, p, log=log)
    out_root = out_root or (REPO_ROOT / "results" / "evaluation")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = out_root / (f"{stamp}_{label}" if label else stamp)
    out.mkdir(parents=True)
    results = []
    for name in names or list(p.scenarios):
        r = run_scenario(settings, p.scenarios[name], scale, seed, pack=p, log=log, **kw)
        results.append(r)
        (out / "scenarios.json").write_text(json.dumps([asdict(x) for x in results], indent=2,
                                                       default=str))
    from cloudpulse.similarity import search
    search.index_all(settings.meta_dsn)  # structural vectors for "similar incidents"
    summary = {
        "environment": envmod.capture(settings.target_dsn, settings.env_label),
        "scale": scale, "seed": seed, "detector": asdict(detector.DetectorConfig()),
        "label": label, "protocol": kw, "delay_offset_corrected": True, "pack": pack,
        "scenarios": [asdict(x) for x in results],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "summary.md").write_text(render_markdown(summary))
    return out


def enrich(out_dir: Path, meta_dsn: str, fault_rounds: int = 8) -> None:
    """Add observed_ratio to a finished evaluation (for runs recorded before the
    field existed) and re-render summary.md. Scenarios ran in order, each on a
    freshly created target database, so the k-th dbid belongs to the k-th scenario."""
    summary = json.loads((out_dir / "summary.json").read_text())
    if not summary.get("delay_offset_corrected"):
        # F-10: runs recorded before the fix reported delays one interval too low.
        for s in summary["scenarios"]:
            s["flagged_after_fault"] = {q: d + 1 for q, d in s["flagged_after_fault"].items()}
            if s.get("detection_delay_rounds") is not None:
                s["detection_delay_rounds"] += 1
        summary["delay_offset_corrected"] = True
    dbids = [r["dbid"] for r in store.fetch_all(
        meta_dsn, "SELECT dbid FROM queries WHERE template_name IS NOT NULL "
                  "GROUP BY dbid ORDER BY min(id)")]
    for s, dbid in zip(summary["scenarios"], dbids):
        if s.get("observed_ratio"):
            continue
        pks = {r["template_name"]: r["id"] for r in store.fetch_all(
            meta_dsn, "SELECT id, template_name FROM queries WHERE dbid = %s "
                      "AND template_name IS NOT NULL", (dbid,))}
        s["observed_ratio"] = observed_ratios(meta_dsn, pks, fault_rounds)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out_dir / "summary.md").write_text(render_markdown(summary))


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
        "| Scenario | Measured effect of the fault on its query | Detected (rounds after fault) "
        "| False positives before fault | Diagnosis | Candidate → verdict |",
        "|---|---|---|---|---|---|",
    ]
    for s in summary["scenarios"]:
        obs0 = s.get("observed_ratio", {}).get(s["expected_query"]) if s["expected_query"] else None
        nothing = obs0 is not None and obs0 < detector.DetectorConfig().min_ratio and not s["detected"]
        det = ("—" if s["expected_query"] is None
               else (f"yes ({s['detection_delay_rounds']})" if s["detected"]
                     else ("nothing to detect (fault effect below 1.5×)" if nothing else "**no**")))
        others = [q for q in s["flagged_after_fault"] if q != s["expected_query"]]
        if others:
            det += f"; also flagged: {', '.join(others)}"
        fp = ", ".join(s["baseline_false_positives"]) or "none"
        diag = ", ".join(s["diagnosis_rules"]) or "—"
        if s["diagnosis_correct"] is not None and not nothing:
            diag += " ✅" if s["diagnosis_correct"] else " ❌"
        obs = s.get("observed_ratio", {}).get(s["expected_query"]) if s["expected_query"] else None
        obs_txt = "—" if obs is None else f"{obs:.2f}× (`{s['expected_query']}`)"
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
                warn = [g for g in (c.get("guardrail") or {}).get("queries", [])
                        if not g.get("collateral_regression") and g.get("ci_low", 0) > 1.0]
                for g in warn:
                    extra += (f"; ⚠ `{g['query']}` {_pct(g['ratio'])} "
                              f"(CI {_pct(g['ci_low'])}…{_pct(g['ci_high'])}, below veto margin)")
                cands.append(f"`{c['description']}` → **{label}**{extra}")
        lines.append(f"| {s['scenario']} | {obs_txt} | {det} | {fp} | {diag} | "
                     f"{'<br>'.join(cands) or '—'} |")
    lines += ["", "Raw evidence (plans, every timed execution, verdict reasons) is in the meta "
              "database and in `results/runs/`."]
    return "\n".join(lines) + "\n"
