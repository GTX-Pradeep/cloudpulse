"""pg_stat_statements collector.

pg_stat_statements exposes *cumulative* counters since its last reset: calls,
total execution time, blocks read, ... A single read says nothing about "now".
The collector therefore stores raw snapshots and derives per-interval deltas:

    interval = snapshot(t2) - snapshot(t1)
    mean latency in the interval = d(total_exec_time) / d(calls)

Things that break a delta, and how they are handled (see the query_intervals view):
  * pg_stat_statements_reset()  -> stats_reset changes            -> interval invalid
  * server crash / restart      -> pg_postmaster_start_time changes -> interval invalid
  * entry evicted and re-added  -> calls go backwards              -> interval invalid
  * collector restart           -> nothing special: the next delta is computed
                                   against the last stored snapshot (longer interval)
Note min/max/stddev columns are cumulative too and cannot be differenced, so the
collector does not use them. Per-interval percentiles are not available from
pg_stat_statements; the detector works on interval means (D-012).
"""

from __future__ import annotations

from cloudpulse import db
from cloudpulse.bench import params as paramsmod
from cloudpulse.bench.spec import QuerySpec
from cloudpulse.meta import store

STATS_SQL = """
SELECT s.queryid, s.dbid, s.userid, s.toplevel, s.query, s.calls, s.total_exec_time,
       s.rows, s.shared_blks_hit, s.shared_blks_read, s.temp_blks_written
FROM pg_stat_statements s
WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND s.queryid IS NOT NULL
"""


def ensure_extension(target_dsn: str) -> None:
    with db.cursor(target_dsn) as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")


def template_queryids(target_dsn: str, templates: list[QuerySpec]) -> dict[int, str]:
    """Map queryid -> template name.

    pg_stat_statements replaces constants with $n, so comparing query text with
    our templates is fragile. Instead we ask PostgreSQL itself: EXPLAIN (VERBOSE)
    reports the "Query Identifier" it computes for the statement, which is the
    same queryid pg_stat_statements uses.
    """
    out: dict[int, str] = {}
    with db.connect(target_dsn) as conn, conn.cursor() as cur:
        for i, q in enumerate(templates):
            p = paramsmod.sample({**q.params, "n": 1}, target_dsn, 0)[0]
            cur.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + q.sql, p)
            plan = cur.fetchone()[0][0]
            qid = plan.get("Query Identifier")
            if qid is not None:
                out[int(qid)] = q.name
    return out


def take_snapshot(target_dsn: str, meta_dsn: str, queryid_names: dict[int, str]) -> int:
    with db.cursor(target_dsn) as cur:
        cur.execute("SELECT pg_postmaster_start_time()")
        pm_start = cur.fetchone()[0]
        cur.execute("SELECT stats_reset, dealloc FROM pg_stat_statements_info")
        stats_reset, dealloc = cur.fetchone()
        cur.execute(STATS_SQL)
        rows = cur.fetchall()

    with db.connect(meta_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO stat_snapshots (stats_reset, dealloc, postmaster_start) "
                "VALUES (%s, %s, %s) RETURNING id",
                (stats_reset, dealloc, pm_start),
            )
            snap_id = cur.fetchone()[0]
            for (qid, dbid, userid, toplevel, text, calls, total, nrows, hit, read, temp) in rows:
                cur.execute(
                    "INSERT INTO queries (queryid, dbid, userid, toplevel, query_text, template_name) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (queryid, dbid, userid, toplevel) DO UPDATE "
                    "SET template_name = coalesce(queries.template_name, EXCLUDED.template_name) "
                    "RETURNING id",
                    (qid, dbid, userid, toplevel, text, queryid_names.get(int(qid))),
                )
                query_pk = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO query_stats_snapshots VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (snap_id, query_pk, calls, total, nrows, hit, read, temp),
                )
        conn.commit()
    return snap_id


def interval_series(meta_dsn: str, query_pk: int) -> list[dict]:
    return store.fetch_all(
        meta_dsn,
        "SELECT snapshot_id, taken_at, calls, mean_ms, exec_time_ms, invalid "
        "FROM query_intervals WHERE query_id = %s ORDER BY snapshot_id",
        (query_pk,),
    )
