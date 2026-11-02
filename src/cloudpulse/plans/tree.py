"""Parse PostgreSQL EXPLAIN (FORMAT JSON) output into a typed tree.

Works for plain EXPLAIN (estimates only) and EXPLAIN (ANALYZE, BUFFERS) output
(estimates + actuals). Fields that are absent stay None, so callers can tell
"zero" apart from "not measured".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class PlanNode:
    node_type: str
    relation: str | None
    index_name: str | None
    alias: str | None
    plan_rows: float  # planner's estimated rows per loop
    total_cost: float
    actual_rows: float | None  # actual rows per loop (EXPLAIN ANALYZE)
    actual_loops: float | None
    actual_total_ms: float | None  # per loop, inclusive of children
    filter: str | None
    index_cond: str | None
    rows_removed_by_filter: float | None
    join_type: str | None
    hash_cond: str | None
    sort_key: list[str] | None
    sort_method: str | None
    sort_space_type: str | None
    sort_space_kb: float | None
    hash_batches: int | None
    disk_usage_kb: float | None
    workers_planned: int | None
    workers_launched: int | None
    shared_hit: int | None
    shared_read: int | None
    temp_written: int | None
    raw: dict[str, Any] = field(repr=False)
    children: list["PlanNode"] = field(default_factory=list)

    # ---- derived -----------------------------------------------------------

    @property
    def analyzed(self) -> bool:
        return self.actual_rows is not None

    @property
    def total_actual_rows(self) -> float | None:
        if self.actual_rows is None:
            return None
        return self.actual_rows * (self.actual_loops or 1)

    @property
    def q_error(self) -> float | None:
        """max(est/act, act/est), both floored at 1 row. 1.0 = perfect estimate."""
        if self.actual_rows is None:
            return None
        est = max(self.plan_rows, 1.0)
        act = max(self.actual_rows, 1.0)
        return max(est / act, act / est)

    @property
    def self_time_ms(self) -> float | None:
        """Exclusive time of this node across all loops (children subtracted)."""
        if self.actual_total_ms is None:
            return None
        total = self.actual_total_ms * (self.actual_loops or 1)
        for c in self.children:
            if c.actual_total_ms is not None and c.node_type not in ("InitPlan", "SubPlan"):
                total -= c.actual_total_ms * (c.actual_loops or 1)
        return max(total, 0.0)

    @property
    def self_temp_written(self) -> int | None:
        """Temp blocks written by this node itself. EXPLAIN's buffer counters are
        inclusive of children, so a spill deep in the tree would otherwise make
        every ancestor look like it spilled too."""
        if self.temp_written is None:
            return None
        return max(self.temp_written - sum(c.temp_written or 0 for c in self.children), 0)

    @property
    def spilled(self) -> bool:
        if self.sort_space_type == "Disk" or (self.sort_method or "").startswith("external"):
            return True
        if self.hash_batches is not None and self.hash_batches > 1:
            return True
        if self.disk_usage_kb:
            return True
        return bool(self.self_temp_written)

    def walk(self) -> Iterator["PlanNode"]:
        yield self
        for c in self.children:
            yield from c.walk()


def _node(d: dict[str, Any]) -> PlanNode:
    loops = d.get("Actual Loops")
    return PlanNode(
        node_type=d.get("Node Type", "?"),
        relation=d.get("Relation Name"),
        index_name=d.get("Index Name"),
        alias=d.get("Alias"),
        plan_rows=float(d.get("Plan Rows", 0)),
        total_cost=float(d.get("Total Cost", 0)),
        actual_rows=d.get("Actual Rows"),
        actual_loops=loops,
        actual_total_ms=d.get("Actual Total Time"),
        filter=d.get("Filter"),
        index_cond=d.get("Index Cond") or d.get("Recheck Cond"),
        rows_removed_by_filter=d.get("Rows Removed by Filter"),
        join_type=d.get("Join Type"),
        hash_cond=d.get("Hash Cond") or d.get("Merge Cond") or d.get("Join Filter"),
        sort_key=d.get("Sort Key"),
        sort_method=d.get("Sort Method"),
        sort_space_type=d.get("Sort Space Type"),
        sort_space_kb=d.get("Sort Space Used"),
        hash_batches=d.get("HashAgg Batches") or d.get("Hash Batches"),
        disk_usage_kb=d.get("Disk Usage"),
        workers_planned=d.get("Workers Planned"),
        workers_launched=d.get("Workers Launched"),
        shared_hit=d.get("Shared Hit Blocks"),
        shared_read=d.get("Shared Read Blocks"),
        temp_written=d.get("Temp Written Blocks"),
        raw={k: v for k, v in d.items() if k != "Plans"},
        children=[_node(c) for c in d.get("Plans", [])],
    )


@dataclass
class Plan:
    root: PlanNode
    planning_ms: float | None
    execution_ms: float | None
    raw: dict[str, Any] = field(repr=False)

    def nodes(self) -> list[PlanNode]:
        return list(self.root.walk())

    @property
    def analyzed(self) -> bool:
        return self.root.analyzed


def parse(explain_json: Any) -> Plan:
    """Accept the value returned by EXPLAIN (FORMAT JSON): a list, a dict or a JSON string."""
    if isinstance(explain_json, str):
        explain_json = json.loads(explain_json)
    if isinstance(explain_json, list):
        explain_json = explain_json[0]
    return Plan(
        root=_node(explain_json["Plan"]),
        planning_ms=explain_json.get("Planning Time"),
        execution_ms=explain_json.get("Execution Time"),
        raw=explain_json,
    )


def shape(node: PlanNode) -> str:
    """Canonical plan *shape*: node types, relations, indexes, join types.
    Excludes costs, row counts and constants, so it only changes when the
    planner picks a structurally different plan."""
    parts = [node.node_type]
    if node.relation:
        parts.append(node.relation)
    if node.index_name:
        parts.append(node.index_name)
    if node.join_type:
        parts.append(node.join_type)
    inner = ",".join(shape(c) for c in node.children)
    return "(" + ":".join(parts) + (f" [{inner}]" if inner else "") + ")"


def fingerprint(plan: Plan | PlanNode) -> str:
    node = plan.root if isinstance(plan, Plan) else plan
    return hashlib.sha1(shape(node).encode()).hexdigest()[:16]
