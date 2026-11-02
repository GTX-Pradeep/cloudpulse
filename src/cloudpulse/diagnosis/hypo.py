"""HypoPG what-if analysis.

HypoPG creates *hypothetical* indexes that exist only in the planner's view of
one session. EXPLAIN (without ANALYZE) then shows whether the planner would use
such an index and what it thinks the cost would be. It cannot run the query, so
it never proves a speed-up. CloudPulse uses it for two cheap pre-checks:

  1. Would the planner use the candidate index for the target query at all?
     If not, a real experiment is pointless.
  2. Which *other* tracked read queries would change plan if the index existed?
     Only those (plus writes on the same table, which always pay index
     maintenance) need benchmarking in the guardrail.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from cloudpulse import db
from cloudpulse.bench.spec import QuerySpec
from cloudpulse.plans import tree

MAX_PARAMS = 60  # EXPLAIN without ANALYZE is cheap: workload sample + heavy hitters
MAX_CHANGED_PARAMS = 10


@dataclass
class HypoResult:
    index_ddl: str
    used_by_target: bool
    target_cost_before: float
    target_cost_after: float
    affected_reads: list[str] = field(default_factory=list)
    writes_on_table: list[str] = field(default_factory=list)
    # parameter values whose plan changes under the hypothetical index; the
    # guardrail benchmark must include them (F-7)
    changed_params: dict[str, list[list]] = field(default_factory=dict)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _plans(cur, q: QuerySpec, params: list[tuple]) -> list[tree.Plan]:
    out = []
    for p in params[:MAX_PARAMS]:
        cur.execute("EXPLAIN (FORMAT JSON) " + q.sql, p)
        out.append(tree.parse(cur.fetchone()[0]))
    return out


def _uses_hypo(plan: tree.Plan) -> bool:
    return any((n.index_name or "").startswith("<") for n in plan.nodes())


def _write_table(q: QuerySpec) -> str | None:
    words = q.sql.split()
    up = [w.upper() for w in words]
    for kw in ("UPDATE", "INTO", "FROM"):
        if kw in up:
            return words[up.index(kw) + 1].strip("(").lower()
    return None


def what_if(
    dsn: str,
    index_ddl: str,
    table: str,
    target: QuerySpec,
    target_params: list[tuple],
    workload: list[QuerySpec],
    workload_params: dict[str, list[tuple]],
) -> HypoResult:
    with db.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS hypopg")
        cur.execute("SELECT hypopg_reset()")
        before_target = _plans(cur, target, target_params)
        before = {q.name: [tree.fingerprint(p) for p in _plans(cur, q, workload_params[q.name])]
                  for q in workload if q.kind == "read" and q.name != target.name}
        cur.execute("SELECT * FROM hypopg_create_index(%s)", (index_ddl,))
        after_target = _plans(cur, target, target_params)
        affected, changes, changed_params = [], {}, {}
        for q in workload:
            if q.kind != "read" or q.name == target.name:
                continue
            plist = workload_params[q.name][:MAX_PARAMS]
            after = [tree.fingerprint(p) for p in _plans(cur, q, plist)]
            changed = [list(p) for p, a, b in zip(plist, after, before[q.name]) if a != b]
            if changed:
                affected.append(q.name)
                changes[q.name] = len(changed)
                changed_params[q.name] = changed[:MAX_CHANGED_PARAMS]
        cur.execute("SELECT hypopg_reset()")
    used = any(_uses_hypo(p) for p in after_target)
    writes = [q.name for q in workload if q.kind == "write" and _write_table(q) == table]
    return HypoResult(
        index_ddl=index_ddl,
        used_by_target=used,
        target_cost_before=sum(p.root.total_cost for p in before_target) / len(before_target),
        target_cost_after=sum(p.root.total_cost for p in after_target) / len(after_target),
        affected_reads=affected,
        writes_on_table=writes,
        changed_params=changed_params,
        details={"plans_changed_per_query": changes, "params_checked": MAX_PARAMS},
    )
