# 4 · Telemetry and regression detection

**Code:** `telemetry/collector.py`, `telemetry/detector.py`, `meta/schema.sql` (`query_intervals` view)

## pg_stat_statements in one paragraph
A PostgreSQL extension, loaded at startup (`shared_preload_libraries`), that keeps
one row per normalised statement: constants become `$1`, `$2`, … It holds
*cumulative* counters since the last reset: calls, total execution time, rows,
block hits and reads, temp blocks, and more. The `queryid` is a hash of the parsed
query, including table OIDs and parameter *types* (D-019).

## Turning cumulative counters into intervals
The collector stores raw snapshots. The `query_intervals` SQL view computes:
```
calls   = calls(t2) - calls(t1)
mean_ms = (total_exec_time(t2) - total_exec_time(t1)) / calls
```
An interval is **invalid** (skipped, never used as a negative delta) when:
- `pg_stat_statements_info.stats_reset` changed (someone reset the statistics);
- `pg_postmaster_start_time()` changed (the server restarted or crashed);
- calls went backwards (the entry was evicted and re-added).

A collector restart needs no special handling: the next delta is computed against
the last stored snapshot, over a longer interval.

## The detector
Each query is compared with **its own** history:
- baseline = up to 20 recent valid intervals that were not anomalous (at least 8,
  else `insufficient_data`);
- robust centre and spread = median and MAD (median absolute deviation), so one
  outlier barely moves them, unlike mean and standard deviation;
- anomaly = z > 4 **and** at least 1.5× the median (statistically unusual *and*
  practically large);
- regression = at least 3 of the last 4 valid intervals are anomalous (a single
  spike is not an incident).

`tests/test_detector.py` checks: no flags on 100 stable noisy series (at most 2
allowed), a step change detected exactly 3 intervals after the step, a single
spike ignored, +30% ignored, a flat baseline not exploding, invalid intervals skipped.

## Ground-truth evaluation
`cloudpulse evaluate` recreates the target, runs the workload for 14 rounds,
injects a fault, and runs 8 more rounds. A **false positive** is any regression
declared before the fault, plus anything declared in the no-fault control scenario.

## Interview questions
1. **Why are pg_stat_statements values cumulative, and what does that force?**
   It is cheap to keep running sums in shared memory. You have to snapshot and
   difference them, and handle resets, restarts and eviction.
2. **Why are fixed thresholds wrong?**
   Every query has its own normal. 500 ms is a disaster for a primary-key lookup
   and normal for a report.
3. **Why median and MAD instead of mean and standard deviation?**
   One extreme interval inflates the mean and standard deviation, hiding later
   anomalies. Median and MAD are robust to a minority of outliers.
4. **What creates false positives here?**
   Changes in parameter mix (a template's mean mixes cheap and expensive values),
   contention from other workloads, and very short intervals with few calls.
5. **Why can a later EXPLAIN ANALYZE differ from the slow execution?**
   Different parameters, cache state, statistics (autovacuum may have run), or
   concurrency. That is why CloudPulse captures quickly, tries several
   parameters, and records whether the plan shape changed since a healthy baseline.
