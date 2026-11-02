# 1 · The measurement bench

**Code:** `datagen/generate.py`, `bench/harness.py`, `bench/params.py`, `bench/env.py`

## What it does
It answers one question reliably: *is version B of the database faster than
version A for these queries?* Everything else in CloudPulse depends on that
answer being trustworthy.

## How it works
1. **Deterministic data.** The data is generated inside PostgreSQL with
   `setseed()` + `random()` in one session, so seed 42 always produces the same
   2,000,000 orders. The skew is deliberate:
   - power-law customer popularity: the top 1% of customers own about 21% of orders;
   - each customer has an activity window, so some heavy customers only have old orders;
   - status depends on age, so `pending` matches only about 0.3% of rows;
   - rows are inserted in `created_at` order, like a real append-only table.
2. **A base database and twins.** The base is built once and frozen
   (`VACUUM FREEZE`). Two file-level copies (`CREATE DATABASE … TEMPLATE`) become
   arm A and arm B, so their statistics and physical layout are identical.
3. **Arms.** B receives the candidate (`CREATE INDEX …`, `ANALYZE …`, or a
   session setting such as `work_mem`).
4. **Warm-up and equivalence check.** Every parameter runs on both arms first;
   read queries must return the same rows on both.
5. **Blocks.** Each block runs the same shuffled parameter list on both arms, in
   order AB, BA, AB, … Every execution is written to `raw.csv`.
6. **Context.** `meta.json` records the host, CPU, PostgreSQL version and key
   settings, the dataset fingerprint, the arms and the parameters. `plans.jsonl`
   stores the plan each arm chose for each parameter.

## Concepts to know
- **Why not one before/after run?** Latency is noisy. Caches, CPU frequency and
  other processes change from second to second. One measurement on each side
  cannot tell an effect apart from noise.
- **Why alternate?** If A always ran first, A would always pay for cold caches.
  Alternating spreads time-dependent effects across both arms.
- **Why parameter mixes?** `WHERE customer_id = $1` costs 0.1 ms for one customer
  and 20 ms for another. A single parameter value describes nothing.
- **Generic vs custom plans.** Prepared statements can switch to a generic plan
  after 5 executions; we disable that so the plan stays stable (D-006).
- **HOT updates.** If an `UPDATE` changes no indexed column and the page has free
  space, PostgreSQL writes a heap-only tuple and touches no index. Indexing a
  frequently updated column turns HOT updates into full updates (F-6, scenario S4).

## Failure cases handled
- A candidate that changes query results → `EquivalenceError`.
- Statement timeouts are recorded as `timeout` rather than dropped.
- Twins are always dropped in a `finally` block; cleanup failures are logged.
- Experiments against the target server are refused (`SafetyError`).

## Interview questions
1. **Why not run `BEGIN; CREATE INDEX; …; ROLLBACK` on production?**
   A plain `CREATE INDEX` takes a SHARE lock and blocks writes until the
   transaction ends, which here is the whole benchmark. The build also warms the
   cache in favour of the "after" side. `CONCURRENTLY` cannot run in a transaction.
2. **How do you know your two databases are identical?**
   They are file-level copies of the same template, and the harness records a
   content fingerprint. An A/A calibration measures any remaining difference.
3. **What is the unit of comparison and why?**
   The block. Executions within a block are correlated, but both arms share that
   block's conditions, so pairing by block cancels the drift.
4. **What would make your benchmark lie?**
   A candidate that changes results (checked), plan changes caused by prepared
   statements (disabled), autovacuum during a run (disabled on lab tables),
   another process on the machine (measured by A/A; this is the residual risk).
5. **Why measure on the client?**
   That is what an application experiences. EXPLAIN ANALYZE timing adds
   instrumentation overhead and skips sending rows to the client.
