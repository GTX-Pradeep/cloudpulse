// Part 1: orientation, the one-page story, how it was built, background concepts.
const { execSync } = require("child_process");
const { REPO } = require("./builder");

function gitLog() {
  const out = execSync(`git -C ${REPO} log --reverse --format=%h%x09%ad%x09%s --date=format:%d-%b-%H:%M`, { encoding: "utf8" });
  return out.trim().split("\n").map((l) => l.split("\t"));
}

module.exports = () => {
  const all = gitLog();
  const commits = all.filter(([, , s]) => !s.startsWith("Checkpoint"));
  const nCheckpoints = all.length - commits.length;
  return [
  { t: "h1", text: "1. How to use this guide", noBreak: true },
  { t: "p", text: "This document explains **everything** in the CloudPulse repository: what problem it solves, every component, every algorithm and formula, every SQL statement that matters, every design decision and its alternatives, every mistake found along the way, every result and where it came from, and how to explain all of it in an interview." },
  { t: "p", text: "Read it in this order the first time:" },
  { t: "steps", ref: "steps1", items: [
    "Chapter 2 (one page): what CloudPulse is and the single result that makes it interesting.",
    "Chapter 4 (concepts): the PostgreSQL and statistics ideas the rest depends on. Do not skip it; every interview question starts here.",
    "Chapter 5 (architecture): the three databases, the end-to-end flow, the module map.",
    "Chapters 6–13 (components): one chapter per part of the system, in the order data flows through it, with the real code.",
    "Chapters 14–15 (results and mistakes): what was measured, how stable it is, and what went wrong. This is your interview story.",
    "Chapters 16–17: testing, CI, reproducibility and honest limitations.",
    "Chapter 18 (interview preparation): pitches, a demo script and a 31-question drill.",
  ] },
  { t: "p", text: "Conventions: `code` is a name in the code or SQL; **bold** marks the idea to remember. Code excerpts are copied automatically from the repository when this document is generated, so they always match the code. Every number in the results chapters is read from a committed results file, and the file is named next to it." },
  { t: "callout", kind: "warn", text: "All measurements in this guide come from the development cloud sandbox: 2 virtual CPUs, 7.8 GB RAM, **PostgreSQL 16.15**. Every results file is labelled `dev-cloud-sandbox (not official)`. CI runs the tests on the pinned PostgreSQL 17 image. Re-run `scripts/reproduce.sh` on your own machine to produce your own numbers." },

  { t: "h1", text: "2. CloudPulse in one page" },
  { t: "h2", text: "The problem" },
  { t: "p", text: "A query that used to take 0.3 ms now takes 180 ms. Most tools stop at *it is slow, maybe add an index*. That leaves the hard questions unanswered:" },
  { t: "bullets", items: [
    "Is this a real regression or normal variation?",
    "What in the execution plan explains it?",
    "Does the proposed fix actually make the query faster, by how much, and how sure are we?",
    "Does the fix make **any other query** slower? An index that helps one query can hurt another, and it always adds work to every write.",
    "Given the evidence, should a human apply it: yes, no, or *we cannot tell*?",
  ] },
  { t: "h2", text: "The idea" },
  { t: "p", text: "CloudPulse treats every proposed fix as a **hypothesis to be tested**, not an answer. Its pipeline is:" },
  { t: "code", text: "Regression -> Evidence -> Diagnosis -> Candidate fix -> Controlled A/B experiment\n           -> Statistical verdict -> Workload-wide guardrail -> SUPPORTED / REJECTED / INCONCLUSIVE" },
  { t: "bullets", items: [
    "**Detect** a regression by comparing each query with its own history (from `pg_stat_statements`).",
    "**Explain** it by capturing the real execution plan (`EXPLAIN ANALYZE`) and extracting evidence from it.",
    "**Hypothesise** a small set of fixes with explainable rules (an index, `ANALYZE`, a scoped `work_mem`).",
    "**Pre-filter** with HypoPG: would the planner even use the index, and which other queries would change plan?",
    "**Experiment** on two byte-identical copies of the database (never the monitored one), alternating between them.",
    "**Decide** with a paired block bootstrap confidence interval and a rule written down before any experiment ran.",
    "**Guard** the rest of the workload: if any other tracked query gets confidently worse by more than 10%, veto the fix.",
  ] },
  { t: "h2", text: "The result that makes it interesting" },
  { t: "p", text: "Scenario S5: the index on `orders(created_at)` is dropped and the recent-orders feed becomes thousands of times slower. CloudPulse proposes re-creating the index, and the experiment confirms it makes the feed **99.7% faster**. But the guardrail finds that, with that index, PostgreSQL answers *latest 10 orders of customer X* by walking the `created_at` index backwards and filtering. For heavy customers whose orders are all old, that walk reads most of the table: up to **56× slower** for one customer, **5.66× slower** for that query overall. Verdict: **REJECTED**, with the reason written out, even though total workload time would drop by 82%." },
  { t: "callout", kind: "key", text: "An optimisation that is excellent for one query can be harmful for the workload. CloudPulse finds that out by measurement, before anyone applies the change to production." },
  { t: "h2", text: "What is real and what is synthetic" },
  { t: "table", headers: ["Real", "Synthetic or limited"], widths: [1, 1], rows: [
    ["PostgreSQL itself: real planner, real execution, real `pg_stat_statements`, real HypoPG and pgvector", "The main dataset is generated (2 M orders, 100 k customers) with deliberate skew"],
    ["Every latency is a real measurement; every execution is stored in `raw.csv`", "The 7 faults were designed by the same author who wrote the rules"],
    ["TPC-H validation uses the official benchmark schema, data generator and queries", "Measurements come from a 2-vCPU cloud VM (noisy), not production hardware"],
    ["Bugs and wrong results were found, fixed and kept on record", "Telemetry intervals are rounds of a workload replay, not wall-clock minutes"],
  ] },

  { t: "h1", text: "3. How the project was built" },
  { t: "p", text: "The project started as a written brief and a guide. The first step was a review of the plan for real technical problems; the main ones found, and how the build addressed each, were:" },
  { t: "table", headers: ["Problem found in the original plan", "How the build resolved it"], widths: [1, 1.2], rows: [
    ["`BEGIN; CREATE INDEX; benchmark; ROLLBACK` on the monitored database takes a SHARE lock that blocks writes for the whole benchmark, and warms caches for arm B", "Experiments run only on a separate lab server, on twin databases cloned from a template (D-003)"],
    ["A read-only workload can never show the main cost of an index (write maintenance, lost HOT updates)", "The tracked workload includes an UPDATE; scenario S4 measures the HOT-update cost"],
    ["`pg_stat_statements` min/max/stddev are cumulative and cannot be differenced; no per-interval percentiles", "Detector works on interval means from differenced sums (D-012)"],
    ["`auto_explain` writes to logs and, with `log_analyze`, instruments every query (perturbs benchmarks)", "On-demand `EXPLAIN ANALYZE` in a read-only transaction with a timeout (D-020)"],
    ["Bootstrapping single executions understates uncertainty; testing many queries inflates false alarms", "Paired **block** bootstrap; Bonferroni-adjusted guardrail (D-010, D-011)"],
    ["Core contribution (experiments) scheduled late in the roadmap", "Built first: measurement bench and statistics before detection and UI"],
  ] },
  { t: "p", text: `Everything was then built, tested and evaluated in one long session, with several rounds of finding and fixing problems in the results. The commit history, in order (${nCheckpoints} further "checkpoint" commits only saved in-progress result files and are omitted):` },
  { t: "table", headers: ["Commit", "When (UTC)", "What changed"], widths: [0.7, 1.1, 5], rows: commits.map(([h, d, s]) => [h, d, s]) },

  { t: "h1", text: "4. Concepts you must know first" },
  { t: "p", text: "This chapter is a primer. Each concept is explained in plain terms, then connected to where CloudPulse uses it." },
  { t: "h2", text: "4.1 How PostgreSQL runs a query" },
  { t: "steps", ref: "steps2", items: [
    "**Parse** the SQL text into a tree, then **rewrite** (views, rules).",
    "**Plan**: the planner enumerates ways to execute the query (which table to scan how, which join method, in which order) and estimates a **cost** for each, using table statistics. It picks the cheapest estimated plan.",
    "**Execute** the plan: a tree of nodes (Seq Scan, Index Scan, Hash Join, Sort, Aggregate, Limit...). Each node pulls rows from its children.",
  ] },
  { t: "p", text: "The planner never runs anything to decide; it **estimates**. When its row-count estimates are wrong, it can choose a plan that is orders of magnitude slower. Most query regressions are a plan choice that stopped being right." },
  { t: "h2", text: "4.2 EXPLAIN and EXPLAIN ANALYZE" },
  { t: "bullets", items: [
    "`EXPLAIN` shows the chosen plan with **estimated** rows and costs. It does not run the query.",
    "`EXPLAIN (ANALYZE, BUFFERS)` **runs** the query and adds actual rows, loops, time per node, and buffer (page) hits, reads and temp writes.",
    "`FORMAT JSON` gives the same as a nested JSON tree, which CloudPulse parses (chapter 7).",
    "Buffer counts and times are **inclusive of children**: a parent's numbers include everything below it. CloudPulse subtracts children to get per-node values (F-2).",
  ] },
  { t: "h2", text: "4.3 Indexes, scans and the cost model" },
  { t: "bullets", items: [
    "A **B-tree index** keeps keys sorted with pointers to table rows. It makes `=`, ranges and `ORDER BY` on its leading column(s) fast.",
    "**Seq Scan** reads the whole table. Cheap per page (sequential I/O), but it reads everything.",
    "**Index Scan** walks the index and fetches matching rows (random I/O per row). **Bitmap Index/Heap Scan** first collects matching row locations, then reads pages in physical order. That is a middle ground for medium selectivity.",
    "The planner chooses using `seq_page_cost`, `random_page_cost`, CPU costs, estimated rows, and the **correlation** between index order and physical row order.",
    "**Composite index** `(a, b)`: usable for filters on `a`, and for `a = ? ORDER BY b` without sorting.",
    "Every index must be updated on INSERT and on any UPDATE that cannot be HOT (4.6). Indexes are never free.",
  ] },
  { t: "h2", text: "4.4 Statistics, ANALYZE and cardinality estimation" },
  { t: "bullets", items: [
    "`ANALYZE` samples rows and stores per-column statistics in `pg_statistic`: null fraction, number of distinct values, a **most-common-values (MCV) list** with frequencies, and a histogram.",
    "From these the planner estimates how many rows a predicate matches (**cardinality** or selectivity).",
    "**q-error** = max(estimated/actual, actual/estimated). 1 is perfect; 10 or more is a serious misestimate.",
    "Statistics go **stale** when much data changes and `ANALYZE` has not run (autovacuum normally runs it). `pg_stat_user_tables.n_mod_since_analyze` counts changed rows since the last analyze.",
    "The planner assumes columns are **independent** and values are **evenly spread**. Correlated data (heavy customers whose orders are all old) breaks this. That is the root of scenario S5.",
  ] },
  { t: "h2", text: "4.5 Join methods and why nested loops can explode" },
  { t: "bullets", items: [
    "**Nested Loop**: for each outer row, look up matching inner rows. Excellent for few outer rows with an index on the inner side; disastrous for many.",
    "**Hash Join**: build a hash table on one side, probe it with the other. Good for large unsorted inputs.",
    "**Merge Join**: both inputs sorted on the join key, merged.",
    "When the planner **under-estimates** outer rows (estimates 1, gets 300,000) it picks a nested loop and does 300,000 index probes. That is scenario S2 (stale statistics).",
  ] },
  { t: "h2", text: "4.6 MVCC, VACUUM and HOT updates" },
  { t: "bullets", items: [
    "PostgreSQL never updates a row in place. An UPDATE writes a **new row version** and marks the old one dead (MVCC). VACUUM later reclaims dead versions.",
    "Normally every index on the table gets a new entry pointing to the new version.",
    "A **HOT (heap-only tuple) update** avoids that: if **no indexed column changes** and the new version **fits on the same page**, no index is touched. This is much cheaper.",
    "`fillfactor` (CloudPulse uses 90 for `orders`) leaves free space on each page so HOT updates have room.",
    "Consequence: adding an index on a column that updates modify (like `updated_at`) turns every such update from HOT into a full update. Verified in this project: 100% HOT without the index, 0% with it (scenario S4, finding F-6).",
    "`VACUUM (FREEZE)` after loading data sets hint bits and the visibility map, so the first benchmark reads do not pay that one-off cost.",
  ] },
  { t: "h2", text: "4.7 work_mem and spills" },
  { t: "p", text: "`work_mem` is the memory one sort or hash operation may use **per operation, per session**. If the data does not fit, the operation **spills** to temporary files (external sort, hash batches). A spill is not automatically slow: spilled files often sit in the operating-system page cache. Finding F-1 shows this: removing a spill did not make the query faster. Raising `work_mem` globally is risky under concurrency (each of N sessions may use it several times), so CloudPulse only ever proposes it **for one query's session**." },
  { t: "h2", text: "4.8 pg_stat_statements and queryid" },
  { t: "bullets", items: [
    "An extension loaded at server start (`shared_preload_libraries`) that keeps one row per **normalised** statement: constants are replaced by `$1`, `$2`...",
    "Counters are **cumulative** since the last reset: `calls`, `total_exec_time`, `rows`, `shared_blks_hit/read`, `temp_blks_written`... You must snapshot and **difference** them to learn about an interval.",
    "`min/max/stddev_exec_time` are cumulative too and cannot be differenced. That is why CloudPulse works on interval means.",
    "`queryid` is a hash of the parsed query tree, including table OIDs **and parameter types**. The same text sent with `int2` vs `int8` parameters becomes two entries (F-4, D-019).",
    "`pg_stat_statements_info` reports `stats_reset` (time of last reset) and `dealloc` (entries evicted because `pg_stat_statements.max` was exceeded).",
    "`EXPLAIN (VERBOSE)` prints the statement's **Query Identifier**, the same queryid. CloudPulse uses it to map entries to its templates.",
  ] },
  { t: "h2", text: "4.9 Prepared statements and generic plans" },
  { t: "p", text: "A prepared statement is planned once and executed many times. After 5 executions PostgreSQL may switch to a **generic plan** that ignores parameter values. psycopg 3 prepares automatically after 5 executions, so a benchmark could silently change plan halfway through. CloudPulse sets `prepare_threshold=None` to disable that (D-006)." },
  { t: "h2", text: "4.10 HypoPG, pgvector, template databases, advisory locks, SKIP LOCKED" },
  { t: "bullets", items: [
    "**HypoPG**: creates *hypothetical* indexes visible only to the planner in one session. `EXPLAIN` (without ANALYZE) then shows whether the planner would use them. It cannot run the query, so it never proves a speed-up.",
    "**pgvector**: a `vector(n)` column type with distance operators (`<=>` is cosine distance). Used for similar-incident search.",
    "**Template databases**: `CREATE DATABASE x TEMPLATE y STRATEGY FILE_COPY` copies y's files. The copy has the same physical row layout **and the same planner statistics**. This is how CloudPulse makes twin databases.",
    "**Advisory locks**: application-defined locks (`pg_advisory_lock(n)`). CloudPulse uses one to run one experiment at a time on the lab server.",
    "**FOR UPDATE SKIP LOCKED**: lock rows you select, skipping rows another transaction has locked. Concurrent workers each grab a different job instead of waiting.",
  ] },
  { t: "h2", text: "4.11 The statistics you need" },
  { t: "bullets", items: [
    "**Median and MAD** (median absolute deviation): robust centre and spread. One huge outlier barely moves them, unlike mean and standard deviation. 1.4826 × MAD estimates the standard deviation for normal data.",
    "**Robust z-score** = (x − median) / (1.4826 × MAD): how unusual a value is.",
    "**Log ratio**: log(B/A). Ratios are multiplicative: 2× faster and 2× slower are symmetric around 0 in log space (−0.69 and +0.69), so averaging log ratios is correct where averaging ratios is not.",
    "**Bootstrap**: estimate uncertainty by resampling your data with replacement many times and recomputing the statistic. The spread of the recomputed values gives a confidence interval.",
    "**Block (paired) bootstrap**: resample whole blocks, not single executions, because executions within a block are correlated. Resampling single executions would give falsely narrow intervals.",
    "**Confidence interval (CI)**: a range that, under the method's assumptions, contains the true value with the stated frequency (e.g. 95%).",
    "**Multiple comparisons and Bonferroni**: testing m queries at 95% each gives about m × 5% chance of at least one false alarm. Bonferroni uses 1 − 0.05/m per query to keep the overall rate near 5%.",
    "**A/A test**: run the experiment with two identical arms. Any difference you see is the method's noise, so this calibrates how far you can trust A/B results.",
    "**Practical significance**: a difference can be statistically real yet too small to matter. CloudPulse requires at least 5% improvement, and vetoes only regressions confidently above 10%.",
  ] },
  ];
};
