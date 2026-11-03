"""Controlled A/B benchmark harness ("the bench").

Design (see docs/decisions.md D-003..D-009 for the reasoning):

* Experiments run on the LAB server, never on the monitored target.
* A *base* database is built deterministically (scale + seed + scenario SQL) and
  then copied twice with CREATE DATABASE ... TEMPLATE. The two copies ("twins")
  are physically identical: same row layout, same planner statistics.
* Arm A's twin gets arm A's setup (usually nothing); arm B's twin gets the
  candidate change (e.g. CREATE INDEX). Session-level candidates such as
  work_mem are applied as session settings instead.
* For each query, measurement is organised in blocks. Every block runs the same
  shuffled parameter list on both arms; the arm order alternates (AB, BA, AB...)
  so slow drift (caches warming, CPU frequency, bloat from writes) hits both arms
  equally. The block is the unit of pairing for the statistics.
* Before measuring, warm-up rounds run every parameter on both arms and (for
  reads) check that both arms return the same rows. A "faster" candidate that
  changes query results is a bug, not an optimisation.
* Every single execution is written to raw.csv, together with meta.json
  (environment, settings, dataset, arms, parameters) and plans.jsonl.
"""

from __future__ import annotations

import csv
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import errors

from cloudpulse import db
from cloudpulse.bench import env as envmod
from cloudpulse.bench import params as paramsmod
from cloudpulse.bench.spec import ArmSpec, ExperimentSpec, QuerySpec
from cloudpulse.config import REPO_ROOT, Settings
from cloudpulse.workload import packs

TWIN_A = "cp_exp_a"
TWIN_B = "cp_exp_b"
MAX_PLAN_PARAMS = 12


class EquivalenceError(RuntimeError):
    """The two arms returned different results for the same query and parameters."""


@dataclass
class ArmSetupReport:
    name: str
    setup_sql: list[str]
    session: dict[str, str]
    setup_seconds: float
    index_bytes: int
    indexes: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    run_dir: Path
    spec: ExperimentSpec
    rows: list[dict]


# --------------------------------------------------------------------------- base


def base_key(scale: str, seed: int, baseline_sql: list[str], pack: str = "ecommerce") -> str:
    h = hashlib.sha256()
    h.update(packs.get(pack).code_fingerprint().encode())
    h.update(json.dumps([scale, seed, baseline_sql]).encode())
    if pack != "ecommerce":  # keep existing e-commerce base names stable
        h.update(pack.encode())
    return h.hexdigest()[:12]


def ensure_base(
    lab_admin_dsn: str, scale: str, seed: int, baseline_sql: list[str], rebuild: bool = False,
    pack: str = "ecommerce",
) -> str:
    """Build (or reuse) the lab base database for a given state. Returns its name.

    The base is keyed by a hash of (generator code, scale, seed, state SQL), so a
    changed scenario or generator can never silently reuse a stale base."""
    key = base_key(scale, seed, baseline_sql, pack)
    p = packs.get(pack)
    name = f"cp_base_{key}"
    if not rebuild and db.database_exists(lab_admin_dsn, name):
        with db.cursor(db.with_database(lab_admin_dsn, name)) as cur:
            cur.execute("SELECT value FROM cloudpulse_dataset WHERE key = 'ready'")
            if cur.fetchone():
                return name
    dsn = p.build(lab_admin_dsn, name, scale, seed)
    with db.cursor(dsn) as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS hypopg")
        # Lab databases must not change underneath an experiment (D-008).
        for table in p.tables:
            cur.execute(f"ALTER TABLE {db.check_ident(table)} SET (autovacuum_enabled = false)")
        for stmt in baseline_sql:
            cur.execute(stmt)
        cur.execute("CHECKPOINT")
        cur.execute("INSERT INTO cloudpulse_dataset VALUES ('base_key', %s), ('ready', 'yes')", (key,))
    return name


# --------------------------------------------------------------------------- arms


def _index_report(cur: psycopg.Cursor) -> tuple[int, list[str]]:
    cur.execute(
        "SELECT coalesce(sum(pg_relation_size(indexrelid)), 0), "
        "coalesce(array_agg(indexrelid::regclass::text ORDER BY 1), '{}') "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname <> 'cloudpulse_dataset'"
    )
    size, names = cur.fetchone()
    return int(size), list(names)


def prepare_twin(lab_admin_dsn: str, base: str, twin: str, arm: ArmSpec) -> ArmSetupReport:
    db.drop_database(lab_admin_dsn, twin)
    db.create_database(lab_admin_dsn, twin, template=base)
    dsn = db.with_database(lab_admin_dsn, twin)
    t0 = time.perf_counter()
    with db.cursor(dsn) as cur:
        for stmt in arm.setup_sql:
            cur.execute(stmt)
        elapsed = time.perf_counter() - t0
        cur.execute("CHECKPOINT")
        size, names = _index_report(cur)
    return ArmSetupReport(arm.name, arm.setup_sql, arm.session, round(elapsed, 3), size, names)


def open_arm_connection(dsn: str, arm: ArmSpec, protocol) -> psycopg.Connection:
    conn = db.connect(dsn)
    with conn.cursor() as cur:
        settings = {**protocol.lab_session, **arm.session}
        settings["statement_timeout"] = f"{protocol.statement_timeout_ms}ms"
        for k, v in settings.items():
            cur.execute("SELECT set_config(%s, %s, false)", (k, str(v)))
    return conn


# --------------------------------------------------------------------------- execution


def _result_hash(rows: list[tuple]) -> str:
    return hashlib.md5(repr(sorted(repr(r) for r in rows)).encode()).hexdigest()


def execute_timed(conn: psycopg.Connection, q: QuerySpec, p: tuple) -> tuple[float, str, int, str | None]:
    """Run one execution. Returns (latency_us, status, rowcount, result_hash)."""
    with conn.cursor() as cur:
        t0 = time.perf_counter_ns()
        try:
            cur.execute(q.sql, p)
            rows = cur.fetchall() if cur.description is not None else None
            t1 = time.perf_counter_ns()
        except errors.QueryCanceled:
            return float("nan"), "timeout", -1, None
        n = len(rows) if rows is not None else cur.rowcount
        h = _result_hash(rows) if (rows is not None and q.compare_results) else None
        return (t1 - t0) / 1000.0, "ok", n, h


def explain(conn: psycopg.Connection, q: QuerySpec, p: tuple) -> dict:
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON) " + q.sql, p)
        return cur.fetchone()[0][0]


def block_order(block: int) -> tuple[str, str]:
    """Counterbalanced arm order: AB, BA, AB, BA, ..."""
    return ("A", "B") if block % 2 == 0 else ("B", "A")


def block_permutation(n: int, seed: int, query_idx: int, block: int) -> list[int]:
    import numpy as np

    rng = np.random.default_rng([seed, query_idx, block])
    return [int(i) for i in rng.permutation(n)]


# --------------------------------------------------------------------------- main


def new_run_dir(name: str, root: Path | None = None) -> Path:
    root = root or (REPO_ROOT / "results" / "runs")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    d = root / f"{stamp}_{name}"
    d.mkdir(parents=True, exist_ok=False)
    return d


def run_experiment(
    spec: ExperimentSpec,
    settings: Settings,
    out_root: Path | None = None,
    rebuild_base: bool = False,
    log=print,
) -> RunResult:
    lab = settings.lab_dsn
    db.assert_not_target(settings, lab)
    log(f"[{spec.name}] preparing base database (scale={spec.scale}, seed={spec.seed})")
    lock = db.connect(lab)
    # One experiment at a time per lab server: twins share names and the machine (D-015).
    lock.execute("SELECT pg_advisory_lock(7346001)")
    try:
        return _run_locked(spec, settings, lab, out_root, rebuild_base, log)
    finally:
        lock.close()


def _run_locked(spec, settings, lab, out_root, rebuild_base, log) -> RunResult:
    proto = spec.protocol
    base = ensure_base(lab, spec.scale, spec.seed, spec.baseline_sql, rebuild=rebuild_base,
                       pack=spec.pack)
    base_dsn = db.with_database(lab, base)
    base_fp = packs.get(spec.pack).fingerprint(base_dsn)
    query_params = {
        q.name: paramsmod.sample(q.params, base_dsn, proto.seed + i)
        for i, q in enumerate(spec.queries)
    }
    env = envmod.capture(base_dsn, settings.env_label)

    run_dir = new_run_dir(spec.name, out_root)
    rows: list[dict] = []
    conns: dict[str, psycopg.Connection] = {}
    try:
        log(f"[{spec.name}] cloning twins and applying arm setup")
        rep_a = prepare_twin(lab, base, TWIN_A, spec.arm_a)
        rep_b = prepare_twin(lab, base, TWIN_B, spec.arm_b)
        conns["A"] = open_arm_connection(db.with_database(lab, TWIN_A), spec.arm_a, proto)
        conns["B"] = open_arm_connection(db.with_database(lab, TWIN_B), spec.arm_b, proto)

        plans_f = open(run_dir / "plans.jsonl", "w")
        # Reads first, then writes: writes change the data the reads would see.
        ordered = sorted(enumerate(spec.queries), key=lambda t: t[1].kind == "write")
        for qi, q in ordered:
            plist = query_params[q.name]
            log(f"[{spec.name}] {q.name}: {len(plist)} params x {proto.blocks} blocks")
            for pi, p in enumerate(plist[:MAX_PLAN_PARAMS]):
                for arm in ("A", "B"):
                    plans_f.write(json.dumps({
                        "query": q.name, "arm": arm, "param_index": pi,
                        "plan": explain(conns[arm], q, p),
                    }, default=str) + "\n")
            # warm-up (discarded) + result-equivalence check
            for w in range(proto.warmup_rounds):
                for p in plist:
                    out = {arm: execute_timed(conns[arm], q, p) for arm in ("A", "B")}
                    if w == 0 and q.kind == "read" and q.compare_results:
                        ha, hb = out["A"][3], out["B"][3]
                        if ha != hb:
                            raise EquivalenceError(
                                f"{q.name}: arms returned different results for params {p}"
                            )
            # measured blocks
            for b in range(proto.blocks):
                perm = block_permutation(len(plist), proto.seed, qi, b)
                for pos, arm in enumerate(block_order(b)):
                    for seq, pi in enumerate(perm):
                        lat, status, n, _ = execute_timed(conns[arm], q, plist[pi])
                        rows.append({
                            "query": q.name, "kind": q.kind, "block": b, "arm": arm,
                            "position": pos, "seq": seq, "param_index": pi,
                            "latency_us": round(lat, 2), "status": status, "rows": n,
                        })
        plans_f.close()

        with open(run_dir / "raw.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        meta = {
            "spec": asdict(spec),
            "environment": env,
            "base_database": base,
            "base_fingerprint": base_fp,
            "arms": {"A": asdict(rep_a), "B": asdict(rep_b)},
            "params": {k: [list(map(str, p)) for p in v] for k, v in query_params.items()},
        }
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
        log(f"[{spec.name}] wrote {len(rows)} measurements to {run_dir}")
        return RunResult(run_dir, spec, rows)
    finally:
        for c in conns.values():
            c.close()
        # Cleanup must not leak experiment databases; failures are reported, not hidden.
        for twin in (TWIN_A, TWIN_B):
            try:
                db.drop_database(lab, twin)
            except psycopg.Error as e:  # pragma: no cover - reported for operator action
                log(f"WARNING: could not drop {twin}: {e}")
