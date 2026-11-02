"""Integration tests against real PostgreSQL servers (docker compose up -d)."""

import dataclasses
import threading
import time

import pytest

from cloudpulse import db
from cloudpulse.bench import harness
from cloudpulse.bench.spec import ExperimentSpec
from cloudpulse.datagen import generate
from cloudpulse.jobs import queue
from cloudpulse.meta import store
from cloudpulse.telemetry import collector
from cloudpulse.workload.suite import BY_NAME

pytestmark = pytest.mark.db


# ---------------------------------------------------------------- data generation


def test_generation_is_deterministic(settings):
    admin = settings.lab_dsn
    a = generate.build_database(admin, "cp_test_gen_a", generate.SCALES["tiny"], 42)
    b = generate.build_database(admin, "cp_test_gen_b", generate.SCALES["tiny"], 42)
    c = generate.build_database(admin, "cp_test_gen_c", generate.SCALES["tiny"], 43)
    try:
        assert generate.fingerprint(a) == generate.fingerprint(b)
        assert generate.fingerprint(a) != generate.fingerprint(c)
        with db.cursor(a) as cur:
            cur.execute(
                "SELECT sum(n) FILTER (WHERE rk <= 20)::float / sum(n) FROM ("
                "SELECT count(*) n, row_number() OVER (ORDER BY count(*) DESC) rk "
                "FROM orders GROUP BY customer_id) s")
            top1pct_share = cur.fetchone()[0]
        assert top1pct_share > 0.15  # power-law skew is present
    finally:
        for n in ("cp_test_gen_a", "cp_test_gen_b", "cp_test_gen_c"):
            db.drop_database(admin, n)


# ---------------------------------------------------------------- harness


def _spec(arm_b_setup, name="t_aa"):
    return ExperimentSpec.from_dict({
        "name": name, "scale": "tiny", "seed": 42, "baseline_sql": [],
        "queries": [{"name": "by_customer",
                     "sql": "SELECT id, amount FROM orders WHERE customer_id = %s",
                     "params": {"type": "customer_mix", "n": 5}}],
        "arm_b": {"setup_sql": arm_b_setup},
        "protocol": {"blocks": 4, "warmup_rounds": 1},
    })


def test_harness_records_every_execution(settings, tmp_path):
    res = harness.run_experiment(_spec([]), settings, out_root=tmp_path, log=lambda *_: None)
    assert len(res.rows) == 4 * 2 * 5
    assert {r["arm"] for r in res.rows} == {"A", "B"}
    firsts = {r["block"]: r["arm"] for r in res.rows if r["position"] == 0}
    assert firsts == {0: "A", 1: "B", 2: "A", 3: "B"}
    assert (res.run_dir / "raw.csv").exists() and (res.run_dir / "meta.json").exists()
    assert not db.database_exists(settings.lab_dsn, harness.TWIN_A)  # cleaned up


def test_harness_rejects_candidate_that_changes_results(settings, tmp_path):
    spec = _spec(["DELETE FROM orders WHERE id % 2 = 0"], name="t_bad")
    with pytest.raises(harness.EquivalenceError):
        harness.run_experiment(spec, settings, out_root=tmp_path, log=lambda *_: None)
    assert not db.database_exists(settings.lab_dsn, harness.TWIN_B)


def test_harness_refuses_target_server(settings, tmp_path):
    bad = dataclasses.replace(settings, lab_dsn=settings.target_dsn)
    with pytest.raises(db.SafetyError):
        harness.run_experiment(_spec([]), bad, out_root=tmp_path, log=lambda *_: None)


# ---------------------------------------------------------------- collector


def test_collector_deltas_and_reset_handling(test_settings):
    s = test_settings
    q = BY_NAME["order_by_id"]
    names = collector.template_queryids(s.target_dsn, [q])
    assert list(names.values()) == ["order_by_id"]

    def run(n):
        with db.connect(s.target_dsn) as conn, conn.cursor() as cur:
            for i in range(1, n + 1):
                cur.execute(q.sql, (i,))
                cur.fetchall()

    with db.cursor(s.target_dsn) as cur:
        cur.execute("SELECT pg_stat_statements_reset()")
    collector.take_snapshot(s.target_dsn, s.meta_dsn, names)
    run(5)
    collector.take_snapshot(s.target_dsn, s.meta_dsn, names)
    run(7)
    collector.take_snapshot(s.target_dsn, s.meta_dsn, names)
    with db.cursor(s.target_dsn) as cur:
        cur.execute("SELECT pg_stat_statements_reset()")
    run(3)
    collector.take_snapshot(s.target_dsn, s.meta_dsn, names)

    qpk = store.fetch_one(s.meta_dsn, "SELECT id FROM queries WHERE template_name = 'order_by_id'")["id"]
    series = collector.interval_series(s.meta_dsn, qpk)
    assert [x["calls"] for x in series][:2] == [7, 3 - 12]
    assert series[0]["invalid"] is False and series[0]["mean_ms"] > 0
    assert series[1]["invalid"] is True  # reset in between: no fake negative delta used


# ---------------------------------------------------------------- job queue


def test_skip_locked_gives_each_worker_a_different_job(test_meta_dsn):
    store.execute(test_meta_dsn, "DELETE FROM jobs")
    ids = {queue.enqueue(test_meta_dsn, "investigate", {"n": i}) for i in range(4)}
    claimed, lock = [], threading.Lock()

    def grab(w):
        j = queue.claim(test_meta_dsn, w)
        with lock:
            claimed.append(j["id"])

    ts = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(claimed) == sorted(ids)
    assert queue.claim(test_meta_dsn, "late") is None


def test_expired_lease_is_reclaimed_then_fails_after_max_attempts(test_meta_dsn):
    store.execute(test_meta_dsn, "DELETE FROM jobs")
    jid = queue.enqueue(test_meta_dsn, "experiment", {}, max_attempts=2)
    j1 = queue.claim(test_meta_dsn, "crashy", lease_seconds=1)
    assert j1["id"] == jid
    assert queue.claim(test_meta_dsn, "other") is None  # lease still valid
    time.sleep(1.2)
    j2 = queue.claim(test_meta_dsn, "other", lease_seconds=1)  # worker "crashed"
    assert j2["id"] == jid and j2["attempts"] == 2
    assert not queue.heartbeat(test_meta_dsn, jid, "crashy")  # old owner lost it
    time.sleep(1.2)
    assert queue.claim(test_meta_dsn, "third") is None  # attempts exhausted
    assert queue.sweep(test_meta_dsn) == 1
    assert store.fetch_one(test_meta_dsn, "SELECT status FROM jobs WHERE id=%s", (jid,))["status"] == "failed"


def test_failed_attempt_is_requeued(test_meta_dsn):
    store.execute(test_meta_dsn, "DELETE FROM jobs")
    jid = queue.enqueue(test_meta_dsn, "experiment", {}, max_attempts=3)
    queue.claim(test_meta_dsn, "w")
    queue.fail(test_meta_dsn, jid, "w", "boom")
    row = store.fetch_one(test_meta_dsn, "SELECT status, last_error FROM jobs WHERE id=%s", (jid,))
    assert row == {"status": "queued", "last_error": "boom"}


# ---------------------------------------------------------------- meta constraints


def test_meta_schema_rejects_impossible_states(test_meta_dsn):
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        store.execute(test_meta_dsn, "INSERT INTO jobs (kind, payload) VALUES ('delete_prod', '{}')")


def test_tracked_queries_belong_to_current_target_only(test_settings):
    """F-8: the same template seen on an older target database must be ignored."""
    from cloudpulse import pipeline
    s = test_settings
    store.execute(s.meta_dsn, "DELETE FROM queries WHERE template_name = 'customer_profile'")
    old = store.execute(s.meta_dsn, "INSERT INTO queries (queryid, dbid, userid, query_text, template_name) "
                                    "VALUES (1, 4242424, 10, 'x', 'customer_profile') RETURNING id")[0]
    cur_dbid = pipeline.target_dbid(s.target_dsn)
    new = store.execute(
        s.meta_dsn,
        "INSERT INTO queries (queryid, dbid, userid, query_text, template_name) "
        "VALUES (2, %s, 10, 'x', 'customer_profile') RETURNING id", (cur_dbid,))[0]
    pks = pipeline.tracked_query_pks(s)
    assert pks["customer_profile"] == new and old not in pks.values()
