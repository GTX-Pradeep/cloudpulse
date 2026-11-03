# Decision log

Every design decision, the alternatives considered, and what was learned when
something did not work. Code comments refer to these IDs (`D-0xx`, `F-x`).

---

## Environment

### D-001 · Three pinned PostgreSQL servers with fixed resources
- **Decision:** `docker-compose.yml` runs `target-db` (monitored), `lab-db`
  (experiments) and `meta-db` (evidence), all from one image based on
  `postgres:17.6-bookworm` plus HypoPG and pgvector. Target and lab are capped at
  2 CPUs / 2 GB; `shared_buffers=256MB`; `jit=off`; `track_io_timing=on`.
- **Why:** the monitored database must never be used for experiments, and
  CloudPulse's own writes must not compete with the workload it measures. Fixed
  resource limits make "the conditions of the measurement" explicit. JIT is off
  because compilation time adds variance to short OLTP queries.
- **Alternatives:** one server with three databases (simpler, but experiments and
  telemetry would share CPU with the monitored workload).
- **Limitations:** Docker on a laptop is still noisy (other apps, CPU frequency
  scaling, thermal limits). The A/A calibration measures how noisy.

### D-002 · Development sandbox ran PostgreSQL 16, not 17
- **Context:** the cloud sandbox used to build this project cannot pull from Docker
  Hub or apt.postgresql.org. It has Ubuntu's PostgreSQL 16 with HypoPG 1.4 and
  pgvector 0.6.
- **Decision:** develop against three local PG16 clusters with the same settings;
  CI and `scripts/reproduce.sh` use the pinned PG17 image.
- **Consequence:** every committed result is labelled
  `dev-cloud-sandbox (not official)` and records `server_version`. Official
  numbers come from re-running on your own machine.

## Measurement (the bench)

### D-003 · Twin databases cloned from a template, not toggling an index
- **Decision:** build a *base* database once, then create two physically identical
  copies with `CREATE DATABASE … TEMPLATE … STRATEGY FILE_COPY`. Arm A's copy stays
  as is; arm B's copy gets the candidate change.
- **Why:** a file-level copy has identical row layout, visibility map *and*
  planner statistics, so the only difference between the arms is the candidate.
  Both arms exist at the same time, so the harness can alternate between them
  cheaply.
- **Alternatives considered:**
  - `BEGIN; CREATE INDEX; benchmark; ROLLBACK` on the monitored database: rejected.
    A plain `CREATE INDEX` holds a SHARE lock that blocks writes for the whole
    benchmark, and the build itself warms the cache in favour of arm B.
  - One database, building and dropping the index between blocks: every switch
    costs a full index build and re-warms the cache.
  - Re-running data generation for each arm: `ANALYZE` samples randomly, so the
    two copies would have slightly different statistics and possibly different plans.
- **Limitation:** the twins share `shared_buffers` and the OS cache. With two
  ~240 MB databases and 256 MB of shared buffers, one arm can evict the other's
  pages. Alternating arms spreads this effect over both.

### D-004 · Counterbalanced blocks; the block is the unit of pairing
- Each block runs the same shuffled parameter list on both arms, in order AB, BA,
  AB, … Slow drift (cache warming, CPU frequency, bloat from writes) therefore
  affects both arms equally, and the statistics compare arms *within* a block.

### D-005 · Result-equivalence check before measuring
- During warm-up, read queries must return the same rows on both arms (multiset
  hash). A "faster" candidate that changes results is a bug, not an optimisation.
  Queries whose results legitimately change under concurrent writes opt out
  (`compare_results=False`).

### D-006 · No automatic server-side prepared statements
- psycopg 3 prepares a statement after 5 executions; PostgreSQL may then switch to
  a *generic plan* that ignores parameter values. That changes what is being
  measured part-way through a run. `prepare_threshold=None` keeps one plan per
  execution (custom plans). Generic-plan regressions are a real problem, but they
  are out of scope for v1.

### D-007 · Latency measured on the client
- `perf_counter_ns` around `execute` plus `fetchall`, on a persistent connection.
  This includes protocol and result transfer, which is what an application sees.
  `EXPLAIN ANALYZE` timing was rejected for benchmarks: it adds per-node
  instrumentation overhead and does not send rows to the client.

### D-008 · Lab tables have autovacuum disabled; data is frozen after load
- Autovacuum could `ANALYZE` one twin halfway through an experiment and change its
  plans, so it is disabled on lab tables (*not* on the target). After generation,
  `VACUUM (FREEZE, ANALYZE)` sets hint bits and the visibility map, so the first
  benchmark reads do not pay that one-off cost.

### D-009 · `synchronous_commit=off` in lab sessions
- Commit latency on a laptop is dominated by `fsync` variance. Turning it off for
  lab sessions keeps write measurements focused on the work an index adds (heap
  and index maintenance, HOT vs non-HOT updates).
- **Limitation:** any extra WAL volume caused by an index shows up only partly in latency.

## Statistics

### D-010 · Effect measure and pre-registered decision rule
- **Effect:** for each block, `d_b = log(sum_B / sum_A)` over the whole parameter
  mix; the estimate is `exp(mean d_b)` (0.25 = four times faster). Using total time
  weights each parameter by its real cost.
- **Uncertainty:** paired block bootstrap (resample whole blocks, 10,000 times),
  percentile interval. Resampling single executions would ignore the correlation
  between neighbouring executions and give falsely narrow intervals.
- **Rule (fixed before any scenario was run):** margin 5%, at least 10 blocks.
  - SUPPORTED if the upper end of the CI is below 0.95.
  - REJECTED if the lower end is above 0.95 (confidently not a meaningful gain).
  - Otherwise, or with too few blocks or timeouts on both sides: INCONCLUSIVE.
  - Timeouts only on the candidate arm: REJECTED.
- **Alternatives:** a t-test on per-execution latencies (assumes independence and
  normality; neither holds); Mann-Whitney (answers "is one distribution shifted",
  not "by how much").

### D-011 · Guardrail rule: Bonferroni-adjusted veto
- Each guardrail query gets a CI at `1 − 0.05/m`. It counts as a collateral
  regression if that CI lies entirely above +10%.
- **Final verdict:** SUPPORTED only if the target is SUPPORTED *and* there is no
  collateral regression. Otherwise REJECTED, with the regressing query named.
- The frequency-weighted change in total workload time is reported but does not
  decide the verdict. The veto is deliberately conservative; a person may decide
  that a regression on a rare query is an acceptable trade.

## Telemetry and detection

### D-012 · Detect on interval means with median/MAD and a sustain rule
- pg_stat_statements only allows differencing cumulative *sums*. `min`, `max` and
  `stddev` are cumulative since reset, so per-interval percentiles are not
  available, and the detector works on interval mean latency.
- **Baseline:** the last 20 valid intervals that were not themselves anomalous;
  at least 8 are needed, otherwise the result is `insufficient_data`.
- **Anomaly:** robust z > 4 using `scale = max(1.4826·MAD, 5%·median, 0.05 ms)`,
  *and* at least 1.5× the median. The floors stop a perfectly flat baseline (MAD = 0)
  from turning tiny wobbles into infinite z-scores.
- **Regression:** at least 3 of the last 4 valid intervals are anomalous.
- **Resets, restarts, eviction:** the interval is marked invalid and skipped. The
  baseline itself is kept, because a statistics reset is a telemetry event, not a
  change in performance.
- **Known limitation:** a template's mean mixes cheap and expensive parameter
  values, so a shift in the parameter mix looks like a regression.

### D-014 · Round-based telemetry in the evaluation
- The scenario runner snapshots after each workload *round* (every template runs at
  least once, in proportion to its weight) instead of every N seconds. That keeps
  a scenario to minutes and ensures rare report queries appear in every interval.
  The collector and detector do not depend on how intervals are defined; a
  deployment would snapshot on a timer.

### D-019 · Integers are always sent as `bigint`
- **Found while testing:** psycopg sends a Python `int` as `int2`, `int4` or `int8`
  depending on its *value*, and pg_stat_statements includes parameter types in the
  queryid. `WHERE id = $1` with id=5 and with id=500000 therefore became two
  separate entries, splitting the query's history. Every connection now
  registers an `int8` dumper for `int`.

### D-020 · Plans captured with on-demand EXPLAIN ANALYZE, not auto_explain
- When an incident opens, CloudPulse runs `EXPLAIN (ANALYZE, BUFFERS)` on the
  target for 5 parameter values inside a `READ ONLY` transaction with a 60 s
  timeout, and keeps the slowest. Writes are only `EXPLAIN`ed, never executed.
- **Why not auto_explain:** it writes to the server log, so it would need log
  shipping and parsing. With `log_analyze` it instruments *every* query, which
  would also perturb the benchmarks.
- **Limitation:** the plan is captured after detection, so cache state,
  statistics or concurrency may differ from the moment the slowdown happened. The
  incident also records whether the plan shape differs from the healthy baseline plan.

## Diagnosis and candidates

### D-013 · A deliberately small candidate space
- Supported candidates: btree indexes with one or two columns, `ANALYZE <table>`,
  and `work_mem` raised **for the affected query's session only** (capped at
  256 MB). No SQL rewriting, no global configuration changes, and no extended
  statistics yet.

### D-022 · HypoPG as a pre-filter and guardrail-scoping tool
- A hypothetical index is created in one lab session. If the planner would not use
  it for the target query, the candidate is filtered out without an experiment.
- The same what-if run lists which *other* tracked read queries would change plan;
  only those, plus writes on the indexed table (which always pay maintenance), go
  into the guardrail.
- HypoPG never proves a speed-up; it only says what the planner *thinks*.

## System

### D-015 · One experiment at a time per lab server
- `pg_advisory_lock` on the lab server serialises experiments. Twins have fixed
  names, and two experiments sharing a 2-CPU machine would measure each other.

### D-016 · Lab state is rebuilt deterministically
- The lab reproduces the target's current state from (generator, scale, seed,
  state SQL). A real deployment would restore a snapshot instead (e.g. a
  `pg_dump` restore or a storage snapshot), which would also need `ANALYZE`
  because statistics are not dumped before PostgreSQL 18.

### D-017 · Job queue in PostgreSQL (`FOR UPDATE SKIP LOCKED` + leases)
- The workload is a handful of jobs that each take minutes, not a high-volume
  message stream. PostgreSQL already gives transactions, durability and row
  locks. Leases plus `max_attempts` handle worker crashes. Redis or Kafka would
  add another system to operate without a measured need.

### D-018 · Deterministic structural vectors, exact search
- 32-dimensional vectors where each dimension has a meaning: SQL structure from
  the PostgreSQL parser (pglast), plan node counts and finding flags. Exact
  cosine search, because at this scale (tens to thousands of incidents) a
  sequential scan is fast and returns exact neighbours. An HNSW or IVFFlat index
  would add recall loss and tuning for no measured benefit.

### D-021 · Server-rendered UI instead of Next.js
- FastAPI + Jinja2 templates + inline SVG. The UI displays evidence; it is not the
  technical core. A separate JavaScript toolchain would double the build and test
  surface for no functional gain.

### D-023 · Replicate verdicts; report UNSTABLE when runs disagree
- **Decision:** `cloudpulse replicate` re-runs every tested candidate (3 extra runs,
  fresh twins, new shuffle seeds). The consolidated verdict is the shared label if all
  runs agree, otherwise **UNSTABLE**, which means a human should decide.
- **Why:** the A/A calibration showed that noise between runs exceeds what a single
  run's interval reflects (F-5), and two runs of the same S4 experiment had already
  disagreed (F-11).
- **Alternatives:** majority vote (hides the disagreement); pooling all blocks into
  one interval (treats runs as interchangeable and ignores run-level variance; a
  hierarchical model would be the principled version).

---

## Failed hypotheses and lessons

### F-1 · "The aggregate spills, so more work_mem will make it faster": false here
While building scenario S3, raising `work_mem` from 4 MB to 64 MB and 256 MB
removed the spill but **did not reduce runtime** (about 2.0–2.5 s in every
setting). The spilled data stayed in the OS page cache, so disk was never the
bottleneck. This is why rules produce hypotheses that are tested, not answers.

### F-2 · False spill findings on parent nodes
EXPLAIN's buffer counters are *inclusive* of children, so a hash aggregate that
wrote temp files made its Sort and Limit parents look like they spilled too. Fixed
by computing per-node temp blocks (`self_temp_written`). Caught by the golden-file tests.

### F-3 · Misestimates reported where they are inherited
Aggregates and joins above a badly estimated scan also show huge q-errors. The
misestimate finding is now emitted only on table scans, where the error originates.

### F-4 · queryid split by parameter types
See D-019. Found by the collector integration test, which could not match a
template to its pg_stat_statements entry.

### F-5 · The A/A confidence intervals are somewhat too narrow on a noisy VM
Committed calibration (`results/calibration/20261102T185543Z`): 10 repeats ×
5 queries, with identical arms and fresh twins each repeat.
- **0 / 50** false SUPPORTED verdicts and **0 / 50** false collateral flags.
- The 95% CI contained 1.0 in **45 / 50** cases (90%, nominal 95%).
- Identical arms differed by up to **17.7%** (`touch_order`, a single-row UPDATE).
  Read queries stayed within about ±15%, mostly within ±8%.

Variation *between* runs (fresh twins, background activity on a shared VM) is
larger than the variation *within* a run that the block bootstrap sees, so
single-run intervals are optimistic. The 5% target margin and the 10%
Bonferroni-adjusted harm margin absorbed this: no wrong verdicts. An earlier,
uncommitted calibration run that overlapped with other heavy work on the same
machine gave 42/50 coverage; it was discarded because the machine was not idle.
**Consequence:** single-run write-latency effects around ±10–15% (such as S4's
+13% on `touch_order`) need replication before being trusted. In S4 the
mechanism is independently confirmed: HOT updates go from 100% to 0% with the index.
Possible improvements: repeat whole experiments, or derive margins from A/A data.

### F-6 · A misleading HOT-update probe
A first check updated a contiguous range of ids and reported 0% HOT updates even
*without* the extra index. Contiguous rows share pages, and the 10% free space per
page filled up immediately. Random single-row updates (what the workload actually
does) showed 100% HOT without the index and 0% with it.

### F-7 · The guardrail missed the `ORDER BY … LIMIT` trap: sampling gap
In the first full evaluation, scenario S5 (re-adding an index on `created_at`)
came back **SUPPORTED with no collateral regression**, although manual probing
had shown that the index makes "latest 10 orders of customer X" 15–60× slower for
heavy customers whose orders are old.
- **Cause:** only a few customers trigger the bad plan. HypoPG checked 6
  parameter values and saw a plan change for 1 of them, so it correctly put the
  query into the guardrail set. But the benchmark then drew its *own* 20-customer
  sample, which contained no trap customer. Every plan recorded for both arms used
  the `customer_id` index, so the guardrail measured only the harmless cases.
- **Fix:** HypoPG now checks the whole 40-value workload sample and records the
  *specific parameter values* whose plan changes. The guardrail benchmark always
  includes those values (`union` sampler). On the S5 lab database, 1 of 40
  customers (customer 23) changes plan.
- **Trade-off:** those values are over-represented in the guardrail sample
  compared with real traffic. The verdict asks "does any parameter regress
  badly?", which is the conservative question for a change applied in production.
- The original run is kept in `results/evaluation/` as recorded. The re-run after
  the fix is a separate, labelled result.

### F-8 · Bookkeeping bug: incidents attributed to an earlier scenario's query
The first full evaluation (`results/evaluation/20261102T184027Z`, kept but marked
superseded) showed "false positives before the fault" in S5 and S6. They were not
real. Each scenario recreates the target database, so the meta database holds one
`queries` row per template *per target database*. `tracked_query_pks` selected
by template name without filtering by database and without an `ORDER BY`, so it
sometimes returned a previous scenario's row. S4's genuine regression was
therefore re-reported in S5 and S6, and two extra incidents were opened. Their
investigations ran against the correct, current database and ended in "no
action", so no verdict was wrong, but the detection bookkeeping was.
- **Fix:** filter by the current target's `dbid` (`pipeline.target_dbid`), with an
  integration test. The full evaluation was re-run with the fix.
- **Lesson:** an identifier that is unique only within a scope (here
  `queryid` per database) needs that scope in every lookup, and the test suite
  needs a case with two scopes.

### F-9 · A random parameter sample never contains the customers who suffer
After the F-7 fix, the corrected run still returned S5 = SUPPORTED. The plan-changing
value HypoPG found in its random 40-customer sample (customer 23) was
benchmarked, and for that customer the new plan was actually **faster**
(4.3 ms → 0.8 ms). The customers who really suffer, heavy customers whose
orders are all old (e.g. customer 7: about 15 ms → 166 ms in manual probing), were
in neither sample.
- **Why:** the planner treats the most frequent values specially (most-common-values
  statistics give them large row estimates), so plan flips concentrate there, but a
  random sample of 40 out of ~100k customers almost never draws them.
- **Fix:** the what-if check always adds the 20 heaviest parameter values
  (`params.heavy_hitters`). On the S5 lab database, HypoPG then reports 21 plan
  changes, including customers 1–9, and those are benchmarked by the guardrail.
- **Lesson:** "test the workload" means testing the *parameter distribution's
  tails*, not only its typical values. Both earlier S5 results are kept as recorded.

### F-10 · Detection delay reported one interval too low
The first workload round has no earlier snapshot to diff against, so each query's
series has one interval fewer than the number of rounds. The evaluation counted
the fault's position from the round count and reported delays one interval too
early. Fixed by counting from the end of the series. Results recorded before the
fix are corrected explicitly (`delay_offset_corrected` in `summary.json`).

### F-11 · Verdicts near a decision margin flip between runs
Replication (`results/replication/20261103T014630Z`, run on a 2.30 GHz host after the
sandbox restarted; the main evaluation ran on a 2.80 GHz host) re-ran the seven tested
candidates three times each.
- **Stable (4 of 4 runs agree):** S5's `created_at` index REJECTED every time
  (collateral 4.4×–5.7×); `ANALYZE orders` SUPPORTED (target 0.43–0.45); the composite
  index SUPPORTED; the S6 `status` index SUPPORTED; `work_mem` REJECTED.
- **UNSTABLE:** S4's `updated_at` index: the UPDATE was **consistently +17% to +23%
  slower in all four runs**, but the lower end of its adjusted CI landed at 1.09–1.15,
  straddling the 1.10 veto line, so the verdict flipped (1 REJECTED, 3 SUPPORTED).
  S1's `customer_id` index: UPDATE +15% to +44% (one run vetoed).
- **Lesson:** the *effect* was reproducible; its *classification* was not, because it
  sits on a threshold. Report the stability of a verdict, not only the verdict.

### F-12 · Hotspot shares above 100% in parallel plans
Found while writing the worked example for the guide: the S5 incident's plan
reported its Seq Scan as a "hotspot" holding 274% of execution time (532 ms of self
time in a 194 ms query). Self time is per-loop time × loops, and parallel workers
count as loops, so in a parallel plan it measures *process* time summed over workers,
not wall-clock time. Dividing it by wall-clock execution time is meaningless.
- **Fix:** shares are now computed against the sum of all nodes' self times (always
  a proper fraction), in both the findings and the UI. A test asserts every hotspot
  share in the golden plans lies in (0, 1].
- **Impact:** hotspot findings are descriptive only (no rule creates a candidate from
  them), so no diagnosis or verdict was affected. Stored findings from earlier runs
  keep their old share values.
