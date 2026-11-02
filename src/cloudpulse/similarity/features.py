"""Deterministic structural vectors for queries and plans (secondary feature).

Instead of an opaque text embedding, each dimension has a meaning ("number of
joins", "has ORDER BY", "plan contains a Seq Scan", "a spill was found"...), so
a similarity result can be explained and the representation is reproducible.

Similar structure is NOT the same root cause. Retrieved incidents are context
("last time a query shaped like this regressed, an index on X was tested and
rejected because ..."), never a diagnosis.
"""

from __future__ import annotations

import math

import numpy as np
from pglast import parse_sql
from pglast.visitors import Visitor

REPR_VERSION = 1
AGGREGATES = {"count", "sum", "avg", "min", "max", "array_agg", "string_agg"}
PLAN_NODES = ["Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Nested Loop",
              "Hash Join", "Merge Join", "Sort", "Aggregate", "Limit", "Gather", "ModifyTable"]
FINDINGS = ["filtered_seq_scan", "misestimate", "spill", "index_scan_heavy_filter",
            "repeated_inner_work"]
SQL_FEATURES = ["tables", "joins", "predicates", "eq_predicates", "range_predicates",
                "aggregates", "group_by", "order_by", "limit", "subqueries", "is_select",
                "is_update", "is_insert", "is_delete", "set_columns"]
NAMES = SQL_FEATURES + [f"plan:{n}" for n in PLAN_NODES] + [f"finding:{f}" for f in FINDINGS]
DIM = 32
assert len(NAMES) <= DIM


class _Counter(Visitor):
    def __init__(self):
        super().__init__()
        self.c = dict.fromkeys(SQL_FEATURES, 0)

    def visit_RangeVar(self, ancestors, node):
        self.c["tables"] += 1

    def visit_JoinExpr(self, ancestors, node):
        self.c["joins"] += 1

    def visit_A_Expr(self, ancestors, node):
        op = node.name[-1].sval if node.name else ""
        if op not in ("=", "<>", "<", ">", "<=", ">=", "~~", "~~*", "!~~"):
            return  # arithmetic such as updated_at + interval '1 second' is not a predicate
        self.c["predicates"] += 1
        if op == "=":
            self.c["eq_predicates"] += 1
        elif op in ("<", ">", "<=", ">="):
            self.c["range_predicates"] += 1

    def visit_FuncCall(self, ancestors, node):
        if node.funcname and node.funcname[-1].sval.lower() in AGGREGATES:
            self.c["aggregates"] += 1

    def visit_SubLink(self, ancestors, node):
        self.c["subqueries"] += 1

    def visit_SelectStmt(self, ancestors, node):
        self.c["is_select"] = 1
        self.c["group_by"] |= bool(node.groupClause)
        self.c["order_by"] |= bool(node.sortClause)
        self.c["limit"] |= node.limitCount is not None

    def visit_UpdateStmt(self, ancestors, node):
        self.c["is_update"] = 1
        self.c["set_columns"] += len(node.targetList or ())

    def visit_InsertStmt(self, ancestors, node):
        self.c["is_insert"] = 1

    def visit_DeleteStmt(self, ancestors, node):
        self.c["is_delete"] = 1


def sql_features(sql: str) -> dict[str, float]:
    # psycopg %s placeholders are not valid SQL for the parser; $n placeholders are.
    n = 0
    out = []
    i = 0
    while i < len(sql):
        if sql.startswith("%s", i):
            n += 1
            out.append(f"${n}")
            i += 2
        else:
            out.append(sql[i])
            i += 1
    v = _Counter()
    v(parse_sql("".join(out)))
    return {k: float(x) for k, x in v.c.items()}


def plan_features(plan_nodes: list[str], finding_kinds: list[str]) -> dict[str, float]:
    f = {f"plan:{n}": float(sum(1 for x in plan_nodes if x == n)) for n in PLAN_NODES}
    f.update({f"finding:{k}": float(k in finding_kinds) for k in FINDINGS})
    return f


def vectorize(features: dict[str, float]) -> list[float]:
    """log1p-scale counts, pad to DIM, L2-normalise (cosine distance then
    compares *shape*, not size)."""
    v = np.zeros(DIM)
    for i, name in enumerate(NAMES):
        v[i] = math.log1p(features.get(name, 0.0))
    norm = np.linalg.norm(v)
    return list(v / norm) if norm else list(v)
