# 3 · Execution plans, diagnosis and candidates

**Code:** `plans/tree.py`, `plans/features.py`, `diagnosis/rules.py`, `diagnosis/hypo.py`

## From EXPLAIN JSON to evidence
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` returns a nested tree. `tree.parse`
turns it into typed `PlanNode`s, from which these values are derived:
- **q-error** = max(estimated/actual, actual/estimated), with both floored at 1 row.
  1.0 is a perfect estimate; above 10 is a serious misestimate.
- **Self time** = the node's total time × loops, minus its children's time.
- **Spill** = an external sort, hash batches > 1, disk usage, or temp blocks
  written by *this* node. Buffer counters are inclusive of children, so we
  subtract (F-2).
- **Plan fingerprint** = a hash of node types, tables, indexes and join types
  only. It changes when the planner picks a different *shape*, not when costs
  or constants change.

`features.extract` produces findings such as "Seq Scan on orders examined
2,000,001 rows and kept 225", "q-error 66,667 on orders", or "HashAggregate
spilled in 129 batches".

## Rules → hypotheses → candidates
| Rule | Evidence | Candidate |
|---|---|---|
| `selective_seq_scan` | Seq Scan ≥ 10k rows, keeps ≤ 5% | index on the filter column(s) |
| `stale_statistics` | q-error ≥ 10 on a scan, and ≥ 5% of rows changed since the last ANALYZE | `ANALYZE table` |
| `estimation_limit` | same misestimate but fresh statistics | none (needs extended statistics or a rewrite) |
| `work_mem_spill` | spill in a sort or hash | `work_mem` for this query's session only |
| `ordered_index_walk` | index walked in order with ≥ 99% of rows filtered (the `ORDER BY … LIMIT` trap) | composite index (filter column, order column) |
| `nested_loop_inner_scan` | ≥ 1000 inner loops over a Seq Scan | index on the join key |
| `cause_unclear` | none of the above | none, and it says so |

Every diagnosis stores its evidence and its **limitations**, so the system can
always answer "why did you suggest this, and what don't you know?"

## HypoPG
A hypothetical index exists only in the planner's view of one session. CloudPulse uses it to:
1. skip candidates the planner would not use at all;
2. find which *other* tracked queries would change plan, to scope the guardrail.

It cannot execute queries, so it never proves a speed-up. Only the lab experiment can.

## Why `ORDER BY created_at DESC LIMIT 10` gets slower with *more* indexes
With an index on `created_at`, the planner can walk orders newest-first and stop
after 10 matching rows. It estimates how far it has to walk by assuming matches
are spread evenly over time. For a heavy customer whose orders are all two years
old, that assumption is badly wrong: it walks most of the table (scenario S5).

## Interview questions
1. **How does PostgreSQL choose between a sequential scan and an index scan?**
   It compares estimated costs: estimated rows (from statistics) × per-row and
   per-page costs (`seq_page_cost`, `random_page_cost`, CPU costs), with
   correlation affecting how random the index's heap access is. Wrong row
   estimates mean wrong choices.
2. **What is a cardinality misestimate, and why does it matter?**
   The planner's guess of how many rows a step produces. Join methods and orders
   are chosen from those guesses. Estimating 1 row when there are 300,000 picks
   a nested loop that runs 300,000 index probes.
3. **Why can nested loops be expensive?**
   The inner side runs once per outer row. That's cheap for a few rows and
   disastrous for many, especially without an index on the inner join key.
4. **What does HypoPG tell you, and what doesn't it?**
   Whether the planner *would* use an index, and its estimated cost. Not real
   runtime, not cache effects, not write cost.
5. **What can't CloudPulse diagnose?**
   Lock contention, I/O saturation, CPU contention, checkpoints, bloat, network,
   generic-plan flips, and anything outside the captured plan. Those get
   `cause_unclear`.
