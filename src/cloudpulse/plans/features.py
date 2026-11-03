"""Evidence extraction from a parsed plan.

Each finding is a small, explainable fact ("Seq Scan on orders read 2,000,000
rows and kept 14") that diagnosis rules can cite. Nothing here decides what to
do; it only describes what the plan shows.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from cloudpulse.plans.tree import Plan, PlanNode

# column references in filter text such as  (customer_id = '42'::bigint)
#   or  ((status = 'pending'::text) AND (o.created_at > ...))
_COL_CMP = re.compile(
    r"\(?(?:\w+\.)?\"?([a-z_][a-z0-9_]*)\"?\s*(=|<>|<=|>=|<|>|~~|IS|= ANY)\s", re.I
)


def filter_columns(expr: str | None) -> list[str]:
    if not expr:
        return []
    seen: list[str] = []
    for m in _COL_CMP.finditer(expr):
        col = m.group(1).lower()
        if col not in seen and col not in ("and", "or", "not"):
            seen.append(col)
    return seen


def equality_columns(expr: str | None) -> list[str]:
    if not expr:
        return []
    return [m.group(1).lower() for m in _COL_CMP.finditer(expr) if m.group(2) in ("=", "= ANY")]


@dataclass
class Finding:
    kind: str
    node_type: str
    relation: str | None
    detail: dict

    def to_dict(self) -> dict:
        return asdict(self)


def _scan_rows_examined(n: PlanNode) -> float | None:
    if n.actual_rows is None:
        return None
    loops = n.actual_loops or 1
    return (n.actual_rows + (n.rows_removed_by_filter or 0)) * loops


def extract(plan: Plan, table_rows: dict[str, float] | None = None) -> list[Finding]:
    """Return findings for an analyzed or estimate-only plan.

    table_rows: approximate row counts per table (pg_class.reltuples), used when
    the plan has no actuals to judge whether a scan is "large".
    """
    table_rows = table_rows or {}
    out: list[Finding] = []
    total_ms = plan.execution_ms
    for n in plan.nodes():
        # 1. sequential scans with a filter (the rule decides if it is "selective")
        if n.node_type in ("Seq Scan", "Parallel Seq Scan") and n.filter:
            examined = _scan_rows_examined(n)
            kept = n.total_actual_rows
            if examined is None:  # estimate-only plan
                examined = table_rows.get(n.relation or "", 0.0)
                kept = n.plan_rows
            if examined:
                out.append(Finding("filtered_seq_scan", n.node_type, n.relation, {
                    "rows_examined": examined,
                    "rows_kept": kept,
                    "selectivity": (kept or 0) / examined if examined else None,
                    "filter": n.filter,
                    "filter_columns": filter_columns(n.filter),
                    "equality_columns": equality_columns(n.filter),
                }))
        # 2. cardinality misestimates at the place they originate: scans of a table.
        #    (Misestimates in joins/aggregates above usually inherit from below.)
        if n.relation and n.analyzed and n.q_error is not None and n.q_error >= 10 and max(
            n.plan_rows, n.actual_rows or 0
        ) >= 1000:
            out.append(Finding("misestimate", n.node_type, n.relation, {
                "estimated_rows": n.plan_rows,
                "actual_rows": n.actual_rows,
                "loops": n.actual_loops,
                "q_error": round(n.q_error, 1),
                "direction": "under" if (n.actual_rows or 0) > n.plan_rows else "over",
                "filter": n.filter or n.index_cond,
            }))
        # 3. spills to disk
        if n.analyzed and n.spilled:
            out.append(Finding("spill", n.node_type, n.relation, {
                "sort_method": n.sort_method,
                "sort_space_kb": n.sort_space_kb,
                "sort_space_type": n.sort_space_type,
                "batches": n.hash_batches,
                "disk_usage_kb": n.disk_usage_kb,
                "temp_written_blocks": n.self_temp_written,
            }))
        # 4. nested loops that repeat expensive inner work
        if n.node_type == "Nested Loop" and n.analyzed and len(n.children) == 2:
            inner = n.children[1]
            loops = inner.actual_loops or 0
            if loops >= 1000:
                out.append(Finding("repeated_inner_work", n.node_type, inner.relation, {
                    "inner_node": inner.node_type,
                    "inner_loops": loops,
                    "inner_total_ms": (inner.actual_total_ms or 0) * loops,
                    "inner_filter": inner.filter or inner.index_cond,
                }))
        # 5. index scan walking an ordered index while filtering heavily (top-N trap)
        if n.node_type in ("Index Scan", "Index Scan Backward", "Index Only Scan") and n.filter:
            examined = _scan_rows_examined(n)
            if examined and examined >= 10_000 and (n.total_actual_rows or 0) / examined < 0.01:
                out.append(Finding("index_scan_heavy_filter", n.node_type, n.relation, {
                    "index": n.index_name,
                    "rows_examined": examined,
                    "rows_kept": n.total_actual_rows,
                    "filter": n.filter,
                    "filter_columns": filter_columns(n.filter),
                }))
        # 6. parallel workers not launched
        if n.workers_planned and n.workers_launched is not None and n.workers_launched < n.workers_planned:
            out.append(Finding("workers_not_launched", n.node_type, n.relation, {
                "planned": n.workers_planned, "launched": n.workers_launched,
            }))
    # 7. hotspots: nodes holding >= 50% of the plan's total node time.
    #    Self time is summed over loops, and parallel workers count as loops, so
    #    in a parallel plan it is *process* time and can exceed wall-clock
    #    execution time (F-12). Shares are therefore taken of the sum of all
    #    nodes' self times, which is always a proper fraction.
    if plan.analyzed and total_ms:
        times = [(n, n.self_time_ms) for n in plan.nodes() if n.self_time_ms is not None]
        node_total = sum(t for _, t in times)
        for n, st in times:
            if node_total and st / node_total >= 0.5:
                out.append(Finding("hotspot", n.node_type, n.relation, {
                    "self_time_ms": round(st, 3), "share": round(st / node_total, 3),
                }))
    return out


def summary(plan: Plan) -> dict:
    nodes = plan.nodes()
    return {
        "node_count": len(nodes),
        "node_types": sorted({n.node_type for n in nodes}),
        "relations": sorted({n.relation for n in nodes if n.relation}),
        "indexes": sorted({n.index_name for n in nodes if n.index_name}),
        "total_cost": plan.root.total_cost,
        "execution_ms": plan.execution_ms,
        "max_q_error": max((n.q_error for n in nodes if n.q_error), default=None),
        "shared_hit": plan.root.shared_hit,
        "shared_read": plan.root.shared_read,
    }
