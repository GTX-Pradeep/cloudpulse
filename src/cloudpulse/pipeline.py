"""End-to-end orchestration:

    monitor round -> regression incident -> plan capture -> diagnosis
    -> candidates -> HypoPG pre-filter -> lab experiment + guardrail -> verdict

Every step writes its evidence to the meta database, so the UI and the
evaluation can show *why* each decision was made.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

import psycopg

from cloudpulse import db
from cloudpulse.bench import harness
from cloudpulse.bench import params as paramsmod
from cloudpulse.bench.spec import ArmSpec, ExperimentSpec, Protocol, QuerySpec
from cloudpulse.config import Settings
from cloudpulse.diagnosis import hypo, rules
from cloudpulse.meta import store
from cloudpulse.plans import features, tree
from cloudpulse.telemetry import collector, detector
from cloudpulse.workload import guardrail
from cloudpulse.workload.suite import BY_NAME, WORKLOAD

PLAN_CAPTURE_PARAMS = 5
PLAN_CAPTURE_TIMEOUT_MS = 60_000
EXPERIMENT_PARAMS_CAP = 20


@dataclass
class LabState:
    """How to reproduce the target's *current* state in the lab.

    In this project the target is built from a known generator plus known SQL,
    so the lab can rebuild it exactly. In a real deployment this would be a
    restored snapshot of the target (D-016)."""
    scale: str
    seed: int
    sql: list[str] = field(default_factory=list)
    db_settings: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- monitoring


def capture_baseline_plans(settings: Settings, query_pks: dict[str, int]) -> None:
    """Record the estimate-only plan shape of each tracked template while healthy."""
    with db.connect(settings.target_dsn) as conn, conn.cursor() as cur:
        for name, qpk in query_pks.items():
            q = BY_NAME[name]
            p = paramsmod.sample({**q.params, "n": 1}, settings.target_dsn, 0)[0]
            cur.execute("EXPLAIN (FORMAT JSON) " + q.sql, p)
            plan = cur.fetchone()[0]
            store.execute(
                settings.meta_dsn,
                "INSERT INTO execution_plans (query_id, source, params, fingerprint, plan) "
                "VALUES (%s, 'baseline_explain', %s, %s, %s)",
                (qpk, store.jsonb(list(p)), tree.fingerprint(tree.parse(plan)), store.jsonb(plan)),
            )


def tracked_query_pks(settings: Settings) -> dict[str, int]:
    rows = store.fetch_all(
        settings.meta_dsn, "SELECT id, template_name FROM queries WHERE template_name IS NOT NULL"
    )
    return {r["template_name"]: r["id"] for r in rows}


def detect_and_open_incidents(
    settings: Settings, cfg: detector.DetectorConfig = detector.DEFAULT_CONFIG,
    scenario: str | None = None,
) -> list[int]:
    opened = []
    for qpk in tracked_query_pks(settings).values():
        series = collector.interval_series(settings.meta_dsn, qpk)
        det = detector.detect(series, cfg)
        if det.state != "regression":
            continue
        try:
            row = store.execute(
                settings.meta_dsn,
                "INSERT INTO performance_incidents (query_id, detector, baseline_median_ms, "
                "baseline_mad_ms, current_ms, ratio, evidence, scenario) "
                "VALUES (%s, 'median_mad_v1', %s, %s, %s, %s, %s, %s) RETURNING id",
                (qpk, det.baseline_median, det.baseline_mad, det.current, det.ratio,
                 store.jsonb({"z": det.z, "anomalous_points": det.anomalous_points,
                              "detected_at_interval": det.index, "config": asdict(cfg),
                              "series": [s["mean_ms"] for s in series]}), scenario),
            )
        except psycopg.errors.UniqueViolation:
            continue  # already has an open incident
        opened.append(row[0])
    return opened


# --------------------------------------------------------------------------- investigation


def _target_context(target_dsn: str) -> rules.Context:
    with db.cursor(target_dsn) as cur:
        cur.execute(
            "SELECT t.relname, ic.relname, "
            "array_agg(a.attname ORDER BY k.ord) "
            "FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid "
            "JOIN pg_class ic ON ic.oid = i.indexrelid "
            "JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, ord) ON true "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
            "JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'public' "
            "GROUP BY 1, 2"
        )
        existing: dict[str, list[list[str]]] = {}
        index_cols: dict[str, list[str]] = {}
        for table, idx, cols in cur.fetchall():
            existing.setdefault(table, []).append(list(cols))
            index_cols[idx] = list(cols)
        cur.execute(
            "SELECT s.relname, c.reltuples, s.n_mod_since_analyze, s.last_analyze, "
            "s.last_autoanalyze, coalesce(NOT ('autovacuum_enabled=false' = ANY(c.reloptions)), true) "
            "FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid"
        )
        stats = {r[0]: {"reltuples": r[1], "n_mod_since_analyze": r[2], "last_analyze": r[3],
                        "last_autoanalyze": r[4], "autovacuum_enabled": r[5]} for r in cur.fetchall()}
        cur.execute("SHOW work_mem")
        work_mem = cur.fetchone()[0]
    return rules.Context(existing, stats, {"work_mem": work_mem}, index_cols)


def capture_incident_plan(settings: Settings, q: QuerySpec) -> tuple[dict, tuple]:
    """Run EXPLAIN (ANALYZE, BUFFERS) on the target for a few parameter values and
    keep the slowest. Reads run in a READ ONLY transaction with a timeout; writes
    are only EXPLAINed (never executed) because ANALYZE would modify data."""
    plist = paramsmod.sample(q.params, settings.target_dsn, 99)[:PLAN_CAPTURE_PARAMS]
    best, best_p, best_ms = None, None, -1.0
    with db.connect(settings.target_dsn, autocommit=False) as conn, conn.cursor() as cur:
        for p in plist:
            if q.kind == "read":
                cur.execute("BEGIN READ ONLY")
                cur.execute(f"SET LOCAL statement_timeout = {PLAN_CAPTURE_TIMEOUT_MS}")
                cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + q.sql, p)
            else:
                cur.execute("BEGIN")
                cur.execute("EXPLAIN (FORMAT JSON) " + q.sql, p)
            plan = cur.fetchone()[0][0]
            conn.rollback()
            ms = plan.get("Execution Time", 0.0)
            if ms > best_ms:
                best, best_p, best_ms = plan, p, ms
    return best, best_p


def investigate(settings: Settings, incident_id: int, lab_state: LabState, log=print) -> list[int]:
    """Collect evidence, diagnose, generate and pre-filter candidates.
    Returns ids of candidates queued for experiments. Idempotent (safe to retry)."""
    inc = store.fetch_one(
        settings.meta_dsn,
        "SELECT i.*, q.template_name, q.id AS qpk FROM performance_incidents i "
        "JOIN queries q ON q.id = i.query_id WHERE i.id = %s", (incident_id,))
    q = BY_NAME[inc["template_name"]]
    store.execute(settings.meta_dsn, "DELETE FROM diagnoses WHERE incident_id = %s", (incident_id,))
    store.execute(settings.meta_dsn, "DELETE FROM candidates WHERE incident_id = %s", (incident_id,))
    store.execute(settings.meta_dsn, "DELETE FROM execution_plans WHERE incident_id = %s", (incident_id,))
    store.execute(settings.meta_dsn,
                  "UPDATE performance_incidents SET status = 'investigating' WHERE id = %s",
                  (incident_id,))

    log(f"[incident {incident_id}] capturing plan for {q.name}")
    raw_plan, p = capture_incident_plan(settings, q)
    plan = tree.parse(raw_plan)
    ctx = _target_context(settings.target_dsn)
    table_rows = {t: float(s["reltuples"]) for t, s in ctx.table_stats.items()}
    finds = features.extract(plan, table_rows)
    base = store.fetch_one(
        settings.meta_dsn,
        "SELECT fingerprint FROM execution_plans WHERE query_id = %s AND source = 'baseline_explain' "
        "ORDER BY captured_at DESC LIMIT 1", (inc["qpk"],))
    # compare estimate-only shapes: re-EXPLAIN without ANALYZE for a fair comparison
    with db.cursor(settings.target_dsn) as cur:
        bp = paramsmod.sample({**q.params, "n": 1}, settings.target_dsn, 0)[0]
        cur.execute("EXPLAIN (FORMAT JSON) " + q.sql, bp)
        now_fp = tree.fingerprint(tree.parse(cur.fetchone()[0]))
    plan_changed = None if base is None else (base["fingerprint"] != now_fp)
    store.execute(
        settings.meta_dsn,
        "INSERT INTO execution_plans (query_id, incident_id, source, params, fingerprint, plan, findings) "
        "VALUES (%s, %s, 'incident_explain_analyze', %s, %s, %s, %s)",
        (inc["qpk"], incident_id, store.jsonb(list(p)), tree.fingerprint(plan),
         store.jsonb(raw_plan), store.jsonb([f.to_dict() for f in finds])))
    store.execute(
        settings.meta_dsn,
        "UPDATE performance_incidents SET evidence = evidence || %s WHERE id = %s",
        (store.jsonb({"plan_changed_since_baseline": plan_changed,
                      "plan_summary": features.summary(plan)}), incident_id))

    diags = rules.diagnose(plan, finds, ctx)
    queued: list[int] = []
    lab = settings.lab_dsn
    db.assert_not_target(settings, lab)
    base_db = None
    for d in diags:
        did = store.execute(
            settings.meta_dsn,
            "INSERT INTO diagnoses (incident_id, rule, hypothesis, evidence, limitations) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (incident_id, d.rule, d.hypothesis, store.jsonb(d.evidence), d.limitations))[0]
        log(f"[incident {incident_id}] diagnosis {d.rule}: {d.hypothesis[:110]}")
        for c in d.candidates:
            hypo_res = None
            status = "queued"
            if c.kind == "index":
                if base_db is None:
                    base_db = harness.ensure_base(lab, lab_state.scale, lab_state.seed, lab_state.sql)
                bdsn = db.with_database(lab, base_db)
                wl_params = {w.name: paramsmod.sample(w.params, bdsn, 5)[:hypo.MAX_PARAMS]
                             for w in WORKLOAD if w.kind == "read"}
                res = hypo.what_if(bdsn, c.setup_sql[0], c.table, q,
                                   paramsmod.sample(q.params, bdsn, 5), WORKLOAD, wl_params)
                hypo_res = res.to_dict()
                if not res.used_by_target:
                    status = "filtered_out"
                log(f"[incident {incident_id}] HypoPG {c.description}: used={res.used_by_target} "
                    f"affected={res.affected_reads} writes={res.writes_on_table}")
            cid = store.execute(
                settings.meta_dsn,
                "INSERT INTO candidates (incident_id, diagnosis_id, kind, description, setup_sql, "
                "session, hypopg, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (incident_id, did, c.kind, c.description, store.jsonb(c.setup_sql),
                 store.jsonb(c.session), store.jsonb(hypo_res) if hypo_res else None, status))[0]
            if status == "queued":
                queued.append(cid)
    if not queued:
        store.execute(settings.meta_dsn,
                      "UPDATE performance_incidents SET status = 'no_action', outcome = %s WHERE id = %s",
                      ("no testable candidate", incident_id))
    return queued


# --------------------------------------------------------------------------- experiments


def guardrail_queries(cand: dict, target: QuerySpec) -> list[QuerySpec]:
    if cand["kind"] == "index" and cand["hypopg"]:
        names = cand["hypopg"]["affected_reads"] + cand["hypopg"]["writes_on_table"]
        return [BY_NAME[n] for n in names if n != target.name]
    if cand["kind"] == "analyze":
        table = cand["description"].split()[-1]
        return [w for w in WORKLOAD
                if w.kind == "read" and w.name != target.name and f" {table} " in f" {w.sql} "]
    return []  # session-scoped setting: only the target query's session is affected


def _cap(q: QuerySpec) -> QuerySpec:
    p = dict(q.params)
    if "n" in p:
        p["n"] = min(int(p["n"]), EXPERIMENT_PARAMS_CAP)
    return QuerySpec(q.name, q.sql, q.kind, p, q.weight, q.compare_results)


def run_candidate_experiment(
    settings: Settings, candidate_id: int, lab_state: LabState,
    protocol: Protocol | None = None, log=print,
) -> dict:
    cand = store.fetch_one(
        settings.meta_dsn,
        "SELECT c.*, q.template_name, i.id AS incident_id FROM candidates c "
        "JOIN performance_incidents i ON i.id = c.incident_id JOIN queries q ON q.id = i.query_id "
        "WHERE c.id = %s", (candidate_id,))
    target = _cap(BY_NAME[cand["template_name"]])
    guard = [_cap(g) for g in guardrail_queries(cand, target)]
    protocol = protocol or Protocol(blocks=15, warmup_rounds=1, seed=4242)
    spec = ExperimentSpec(
        name=f"cand{candidate_id}_{target.name}",
        description=cand["description"],
        scale=lab_state.scale, seed=lab_state.seed, baseline_sql=lab_state.sql,
        queries=[target, *guard],
        arm_a=ArmSpec("A", [], dict(lab_state.db_settings)),
        arm_b=ArmSpec("B", list(cand["setup_sql"]), {**lab_state.db_settings, **cand["session"]}),
        protocol=protocol, target_query=target.name,
    )
    store.execute(settings.meta_dsn, "DELETE FROM experiments WHERE candidate_id = %s", (candidate_id,))
    exp_id = store.execute(
        settings.meta_dsn,
        "INSERT INTO experiments (candidate_id, protocol) VALUES (%s, %s) RETURNING id",
        (candidate_id, store.jsonb(asdict(protocol))))[0]
    try:
        res = harness.run_experiment(spec, settings, log=log)
    except Exception as e:
        store.execute(settings.meta_dsn,
                      "UPDATE experiments SET status='failed', error=%s, finished_at=now() WHERE id=%s",
                      (repr(e), exp_id))
        store.execute(settings.meta_dsn, "UPDATE candidates SET status='failed' WHERE id=%s",
                      (candidate_id,))
        raise
    report = guardrail.evaluate(res.rows, target, guard)
    meta = (res.run_dir / "meta.json").read_text()
    with db.connect(settings.meta_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            with cur.copy("COPY experiment_runs (experiment_id, query_name, block, arm, position, "
                          "seq, param_index, latency_us, status, rows) FROM STDIN") as cp:
                for r in res.rows:
                    lat = r["latency_us"]
                    cp.write_row((exp_id, r["query"], r["block"], r["arm"], r["position"], r["seq"],
                                  r["param_index"], None if lat != lat else lat, r["status"], r["rows"]))
            cur.execute(
                "UPDATE experiments SET status='done', finished_at=now(), run_dir=%s, "
                "environment=(%s::jsonb)->'environment' WHERE id=%s",
                (str(res.run_dir), meta, exp_id))
            cur.execute(
                "INSERT INTO verdicts (experiment_id, label, reason, target, guardrail) "
                "VALUES (%s, %s, %s, %s, %s)",
                (exp_id, report.label, report.reason, store.jsonb(report.target),
                 store.jsonb({"queries": report.guardrail, "collateral": report.collateral,
                              "weighted_change_pct": report.weighted_change_pct,
                              "warnings": report.warnings})))
            cur.execute("UPDATE candidates SET status='tested' WHERE id=%s", (candidate_id,))
        conn.commit()
    (res.run_dir / "verdict.json").write_text(__import__("json").dumps(report.to_dict(), indent=2))
    log(f"[candidate {candidate_id}] {report.label}: {report.reason}")
    _maybe_close_incident(settings, cand["incident_id"])
    return report.to_dict()


def _maybe_close_incident(settings: Settings, incident_id: int) -> None:
    rows = store.fetch_all(
        settings.meta_dsn,
        "SELECT c.description, c.status, v.label FROM candidates c "
        "LEFT JOIN experiments e ON e.candidate_id = c.id LEFT JOIN verdicts v ON v.experiment_id = e.id "
        "WHERE c.incident_id = %s AND c.status <> 'filtered_out'", (incident_id,))
    if any(r["status"] == "queued" for r in rows):
        return
    supported = [r["description"] for r in rows if r["label"] == "SUPPORTED"]
    if supported:
        status, outcome = "resolved", "SUPPORTED: " + "; ".join(supported) + " (awaiting human review)"
    else:
        labels = sorted({r["label"] or r["status"] for r in rows})
        status, outcome = "no_action", "no candidate supported (" + ", ".join(labels) + ")"
    store.execute(settings.meta_dsn,
                  "UPDATE performance_incidents SET status=%s, outcome=%s WHERE id=%s",
                  (status, outcome, incident_id))


def wait(seconds: float) -> None:  # pragma: no cover - tiny helper for scripts
    time.sleep(seconds)


Log = Callable[[str], None]
