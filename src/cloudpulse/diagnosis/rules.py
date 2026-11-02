"""Explainable diagnosis rules and candidate generation.

A rule turns plan evidence into a *hypothesis* and, when we support that kind of
fix, into a concrete *candidate*. Nothing here claims a fix works; the
experiment engine decides that. Every diagnosis records the evidence it used and
what it cannot know, so "why did you suggest this?" always has an answer.

Supported candidate kinds (deliberately small, D-013):
  index    single-column or two-column btree index
  analyze  refresh planner statistics for one table
  setting  work_mem raised for the affected query's session only (never globally)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from cloudpulse.plans.features import Finding, equality_columns, filter_columns
from cloudpulse.plans.tree import Plan

MIN_SCAN_ROWS = 10_000
MAX_SELECTIVITY = 0.05
WORK_MEM_CAP_MB = 256


@dataclass
class Candidate:
    kind: str
    description: str
    setup_sql: list[str] = field(default_factory=list)
    session: dict[str, str] = field(default_factory=dict)
    table: str | None = None
    columns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Diagnosis:
    rule: str
    hypothesis: str
    evidence: dict
    limitations: str
    candidates: list[Candidate] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["candidates"] = [c.to_dict() for c in self.candidates]
        return d


@dataclass
class Context:
    """Facts about the target that rules may need besides the plan."""
    existing_indexes: dict[str, list[list[str]]]  # table -> list of column lists
    table_stats: dict[str, dict]  # table -> reltuples, n_mod_since_analyze, last_analyze...
    settings: dict[str, str]  # e.g. work_mem
    index_columns: dict[str, list[str]] = field(default_factory=dict)  # index name -> columns


def _index_name(table: str, cols: list[str]) -> str:
    return f"cp_{table}_{'_'.join(cols)}_idx"[:63]


def _covered(ctx: Context, table: str, cols: list[str]) -> bool:
    for existing in ctx.existing_indexes.get(table, []):
        if existing[: len(cols)] == cols:
            return True
    return False


def index_candidate(table: str, cols: list[str]) -> Candidate:
    name = _index_name(table, cols)
    return Candidate(
        kind="index",
        description=f"CREATE INDEX ON {table} ({', '.join(cols)})",
        setup_sql=[f"CREATE INDEX {name} ON {table} ({', '.join(cols)})"],
        table=table,
        columns=cols,
    )


def _mb(kb: float) -> int:
    mb = 1
    while mb * 1024 < kb:
        mb *= 2
    return mb


# --------------------------------------------------------------------------- rules


def rule_selective_seq_scan(f: Finding, ctx: Context) -> Diagnosis | None:
    d = f.detail
    if f.kind != "filtered_seq_scan" or not f.relation:
        return None
    if d["rows_examined"] < MIN_SCAN_ROWS or (d["selectivity"] or 1) > MAX_SELECTIVITY:
        return None
    eq = d["equality_columns"]
    rng = [c for c in d["filter_columns"] if c not in eq]
    cols = (eq[:1] + rng[:1]) if eq else rng[:1]
    if not cols:
        return None
    diag = Diagnosis(
        rule="selective_seq_scan",
        hypothesis=(
            f"Seq Scan on {f.relation} examined {d['rows_examined']:,.0f} rows to keep "
            f"{d['rows_kept'] or 0:,.0f} ({(d['selectivity'] or 0):.2%}). An index on "
            f"{f.relation}({', '.join(cols)}) could let PostgreSQL read only matching rows."
        ),
        evidence=d,
        limitations=(
            "Selectivity comes from one captured execution and its parameters; other "
            "parameter values may be less selective. An index also costs storage and "
            "write maintenance, which the guardrail must measure."
        ),
    )
    if _covered(ctx, f.relation, cols):
        diag.limitations += " An index with these leading columns already exists but was not used."
    else:
        diag.candidates.append(index_candidate(f.relation, cols))
    return diag


def rule_misestimate(f: Finding, ctx: Context) -> Diagnosis | None:
    if f.kind != "misestimate" or not f.relation:
        return None
    st = ctx.table_stats.get(f.relation, {})
    reltuples = max(float(st.get("reltuples") or 0), 1.0)
    mod = float(st.get("n_mod_since_analyze") or 0)
    stale = mod / reltuples >= 0.05
    d = {**f.detail, "n_mod_since_analyze": mod, "reltuples": reltuples,
         "last_analyze": st.get("last_analyze"), "last_autoanalyze": st.get("last_autoanalyze"),
         "autovacuum_enabled": st.get("autovacuum_enabled")}
    if stale:
        return Diagnosis(
            rule="stale_statistics",
            hypothesis=(
                f"The planner estimated {f.detail['estimated_rows']:,.0f} rows on {f.relation} but "
                f"saw {f.detail['actual_rows']:,.0f} (q-error {f.detail['q_error']}). "
                f"{mod:,.0f} rows changed since the last ANALYZE ({mod / reltuples:.0%} of the table), "
                "so statistics are likely stale."
            ),
            evidence=d,
            limitations="A fresh ANALYZE samples rows randomly; rare values can still be missed.",
            candidates=[Candidate("analyze", f"ANALYZE {f.relation}",
                                  setup_sql=[f"ANALYZE {f.relation}"], table=f.relation)],
        )
    return Diagnosis(
        rule="estimation_limit",
        hypothesis=(
            f"Large row-count misestimate on {f.relation} (q-error {f.detail['q_error']}) although "
            "statistics look fresh: likely correlated columns or an expression the planner cannot estimate."
        ),
        evidence=d,
        limitations="No supported automatic candidate (extended statistics or query rewrite needed).",
    )


def rule_spill(f: Finding, ctx: Context, plan: Plan) -> Diagnosis | None:
    if f.kind != "spill":
        return None
    node = next((n for n in plan.nodes() if n.node_type == f.node_type and n.spilled), None)
    peak = float((node.raw.get("Peak Memory Usage") if node else 0) or 0)
    disk = float(f.detail.get("disk_usage_kb") or f.detail.get("sort_space_kb") or 0)
    need_mb = min(_mb((peak + disk) * 1.5), WORK_MEM_CAP_MB)
    current = ctx.settings.get("work_mem", "?")
    return Diagnosis(
        rule="work_mem_spill",
        hypothesis=(
            f"{f.node_type} spilled to disk (batches={f.detail.get('batches')}, "
            f"disk={disk:,.0f} kB) with work_mem={current}. More memory for this query may avoid the spill."
        ),
        evidence={**f.detail, "peak_memory_kb": peak, "work_mem": current},
        limitations=(
            "Avoiding a spill only helps if disk I/O is the bottleneck; spilled data often "
            "stays in the OS cache. work_mem is per operation per session, so the candidate "
            "is scoped to this query's session and must never be applied globally without "
            "checking concurrency."
        ),
        candidates=[Candidate("setting", f"SET work_mem = '{need_mb}MB' for this query's session",
                              session={"work_mem": f"{need_mb}MB"})],
    )


def rule_repeated_inner(f: Finding, ctx: Context) -> Diagnosis | None:
    if f.kind != "repeated_inner_work" or not f.relation:
        return None
    if f.detail.get("inner_node") not in ("Seq Scan", "Parallel Seq Scan"):
        return None
    cols = equality_columns(f.detail.get("inner_filter"))[:1]
    diag = Diagnosis(
        rule="nested_loop_inner_scan",
        hypothesis=(
            f"A nested loop rescans {f.relation} {f.detail['inner_loops']:,.0f} times. An index "
            f"on the join key {cols} could turn each rescan into an index lookup."
        ),
        evidence=f.detail,
        limitations="The join key is inferred from the filter text and may be incomplete.",
    )
    if cols and not _covered(ctx, f.relation, cols):
        diag.candidates.append(index_candidate(f.relation, cols))
    return diag


def rule_index_heavy_filter(f: Finding, ctx: Context) -> Diagnosis | None:
    if f.kind != "index_scan_heavy_filter" or not f.relation:
        return None
    eq = [c for c in filter_columns(f.detail.get("filter")) if c][:1]
    order_cols = ctx.index_columns.get(f.detail.get("index") or "", [])[:1]
    cols = eq + [c for c in order_cols if c not in eq]
    diag = Diagnosis(
        rule="ordered_index_walk",
        hypothesis=(
            f"{f.node_type} on {f.detail.get('index')} walked {f.detail['rows_examined']:,.0f} rows "
            f"and filtered out almost all of them. The planner likely assumed matching rows are "
            f"spread evenly; a composite index {f.relation}({', '.join(cols)}) would find them directly."
        ),
        evidence=f.detail,
        limitations="Typical of ORDER BY ... LIMIT with correlated data; depends on parameter values.",
    )
    if len(cols) == 2 and not _covered(ctx, f.relation, cols):
        diag.candidates.append(index_candidate(f.relation, cols))
    return diag


def diagnose(plan: Plan, findings: list[Finding], ctx: Context) -> list[Diagnosis]:
    out: list[Diagnosis] = []
    for f in findings:
        for rule in (rule_selective_seq_scan, rule_misestimate, rule_repeated_inner,
                     rule_index_heavy_filter):
            d = rule(f, ctx)
            if d:
                out.append(d)
        if f.kind == "spill":
            d = rule_spill(f, ctx, plan)
            if d:
                out.append(d)
    # de-duplicate candidates across diagnoses
    seen: set[str] = set()
    for d in out:
        uniq = []
        for c in d.candidates:
            if c.description not in seen:
                seen.add(c.description)
                uniq.append(c)
        d.candidates = uniq
    if not out:
        hot = [f.to_dict() for f in findings if f.kind == "hotspot"]
        out.append(Diagnosis(
            rule="cause_unclear",
            hypothesis="No supported, planner-visible cause was found in the captured plan.",
            evidence={"findings": [f.to_dict() for f in findings], "hotspots": hot},
            limitations=(
                "The slowdown may come from outside the plan: CPU or I/O contention, locks, "
                "checkpoints, bloat, or a cause CloudPulse has no rule for."
            ),
        ))
    return out
