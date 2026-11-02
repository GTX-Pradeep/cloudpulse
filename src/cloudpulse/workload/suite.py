"""The tracked workload and the ground-truth regression scenarios.

The *tracked workload* is the set of query templates an application runs, with
a parameter sampler and a relative frequency (weight) for each. CloudPulse uses
it to (a) generate load on the target, (b) map pg_stat_statements entries back
to runnable templates, and (c) choose which queries the guardrail must check.

Each *scenario* injects one known fault into a healthy database. Because we know
the cause, we can score detection, diagnosis and the final verdict. The expected
diagnosis is ground truth; the expected verdict is NOT asserted: the experiment
decides that, and the evaluation reports what actually happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cloudpulse.bench.spec import QuerySpec

WORKLOAD: list[QuerySpec] = [
    QuerySpec(
        "orders_by_customer",
        "SELECT id, amount, created_at FROM orders WHERE customer_id = %s",
        params={"type": "customer_mix", "n": 40}, weight=30,
    ),
    QuerySpec(
        "latest_orders_for_customer",
        "SELECT id, amount, created_at FROM orders WHERE customer_id = %s "
        "ORDER BY created_at DESC LIMIT 10",
        params={"type": "customer_mix", "n": 40}, weight=20,
    ),
    QuerySpec(
        "recent_orders_feed",
        "SELECT id, customer_id, amount FROM orders WHERE created_at > %s "
        "ORDER BY created_at DESC LIMIT 50",
        params={"type": "hours_before_end", "n": 20, "max_hours": 48}, weight=5,
    ),
    QuerySpec(
        "order_by_id",
        "SELECT id, customer_id, status, amount, created_at FROM orders WHERE id = %s",
        params={"type": "order_ids", "n": 100}, weight=30,
    ),
    QuerySpec(
        "customer_profile",
        "SELECT id, name, country, segment FROM customers WHERE id = %s",
        params={"type": "customer_mix", "n": 40}, weight=10,
    ),
    QuerySpec(
        "segment_revenue_for_status",
        "SELECT c.segment, count(*), sum(o.amount) FROM orders o "
        "JOIN customers c ON c.id = o.customer_id WHERE o.status = %s GROUP BY c.segment",
        params={"type": "values", "values": [["backordered"], ["returned"], ["cancelled"]]},
        weight=1,
    ),
    QuerySpec(
        "monthly_revenue_top_customers",
        "SELECT customer_id, date_trunc('month', created_at) AS month, sum(amount) AS revenue, "
        "count(*) AS n FROM orders WHERE created_at >= '2025-01-01' AND created_at < '2026-01-01' "
        "GROUP BY 1, 2 ORDER BY revenue DESC LIMIT 20",
        params={"type": "none", "n": 1}, weight=0.2,
    ),
    QuerySpec(
        "recently_updated_orders",
        "SELECT id, status, updated_at FROM orders WHERE updated_at > %s "
        "ORDER BY updated_at DESC LIMIT 100",
        params={"type": "hours_before_end", "n": 20, "max_hours": 24}, weight=2,
        # concurrent writes move updated_at, so results legitimately differ over time
        compare_results=False,
    ),
    QuerySpec(
        "touch_order",
        "UPDATE orders SET updated_at = updated_at + interval '1 second' WHERE id = %s",
        kind="write", params={"type": "order_ids", "n": 200}, weight=20,
    ),
]

BY_NAME = {q.name: q for q in WORKLOAD}

# Healthy starting point shared by every scenario (applied after data generation).
BASELINE_SQL = [
    "CREATE INDEX orders_customer_id_idx ON orders (customer_id)",
    "CREATE INDEX orders_created_at_idx ON orders (created_at)",
    "CREATE INDEX orders_updated_at_idx ON orders (updated_at)",
    "ANALYZE orders",
]
# Database-level configuration of the healthy target.
BASELINE_DB_SETTINGS = {"work_mem": "64MB"}

BACKORDER_SQL = (
    "INSERT INTO orders (id, customer_id, status, amount, created_at, updated_at) "
    "SELECT m.max_id + g, 1 + (g::bigint * 7919) % m.n_customers, 'backordered', 10, "
    "timestamptz '2025-12-20' + g * interval '1 second', "
    "timestamptz '2025-12-20' + g * interval '1 second' "
    "FROM (SELECT (SELECT max(id) FROM orders) AS max_id, "
    "             (SELECT count(*) FROM customers) AS n_customers) m, "
    "generate_series(1, 300000) g"
)


@dataclass
class Scenario:
    name: str
    title: str
    fault_sql: list[str] = field(default_factory=list)
    fault_db_settings: dict[str, str] = field(default_factory=dict)
    noisy_neighbor: bool = False
    regressed_query: str | None = None
    expected_diagnosis: str | None = None  # diagnosis rule that should explain it
    expected_candidate: str | None = None  # substring of the right candidate
    story: str = ""


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario(
            "s0_control", "No fault (false-positive control)",
            story="Nothing changes. Any regression flagged here is a false positive.",
        ),
        Scenario(
            "s1_dropped_fk_index", "Index on orders.customer_id dropped",
            fault_sql=["DROP INDEX orders_customer_id_idx"],
            regressed_query="orders_by_customer",
            expected_diagnosis="selective_seq_scan",
            expected_candidate="(customer_id)",
            story="A cleanup migration drops an index it believed unused.",
        ),
        Scenario(
            "s2_stale_statistics", "Bulk load without ANALYZE",
            fault_sql=[
                "ALTER TABLE orders SET (autovacuum_enabled = false)",
                BACKORDER_SQL,
            ],
            regressed_query="segment_revenue_for_status",
            expected_diagnosis="stale_statistics",
            expected_candidate="ANALYZE",
            story="300k back-orders are bulk loaded while autovacuum is off for the "
                  "table. The planner still believes status='backordered' matches ~1 row.",
        ),
        Scenario(
            "s3_work_mem_lowered", "work_mem lowered from 64MB to 4MB",
            fault_db_settings={"work_mem": "4MB"},
            regressed_query="monthly_revenue_top_customers",
            expected_diagnosis="work_mem_spill",
            expected_candidate="work_mem",
            story="A config change lowers work_mem; a reporting aggregate starts spilling.",
        ),
        Scenario(
            "s4_updated_at_index_dropped", "Index on orders.updated_at dropped",
            fault_sql=["DROP INDEX orders_updated_at_idx"],
            regressed_query="recently_updated_orders",
            expected_diagnosis="selective_seq_scan",
            expected_candidate="(updated_at)",
            story="The index is dropped (it slowed down writes). A dashboard query regresses. "
                  "Re-adding the index fixes the read but makes every touch_order update "
                  "non-HOT: the guardrail must surface that trade-off.",
        ),
        Scenario(
            "s5_created_at_index_dropped", "Index on orders.created_at dropped",
            fault_sql=["DROP INDEX orders_created_at_idx"],
            regressed_query="recent_orders_feed",
            expected_diagnosis="selective_seq_scan",
            expected_candidate="(created_at)",
            story="Dropping the index slows the recent-orders feed. Re-adding it fixes the "
                  "feed, but the planner then walks it for 'latest orders of customer X' and "
                  "filters, which is far slower for heavy customers whose orders are old.",
        ),
        Scenario(
            "s6_noisy_neighbor", "CPU/IO contention from another tenant",
            noisy_neighbor=True,
            story="Heavy unrelated queries compete for the machine. Plans do not change; "
                  "there is no index or statistics fix. CloudPulse should say so.",
        ),
    ]
}
