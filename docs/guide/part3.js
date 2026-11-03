// Part 3: results (read from committed files), mistakes, testing, limitations,
// interview preparation, glossary, appendices.
const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");
const { REPO, fileText } = require("./builder");

const J = (p) => JSON.parse(fs.readFileSync(path.join(REPO, p), "utf8"));
const latest = (glob) => {
  const out = execSync(`ls -d ${REPO}/${glob} 2>/dev/null || true`, { encoding: "utf8" }).trim().split("\n").filter(Boolean).sort();
  return out.length ? path.relative(REPO, out[out.length - 1]) : null;
};
const pct = (r) => `${r < 1 ? "−" : "+"}${Math.abs((r - 1) * 100).toFixed(1)}%`;
const f2 = (x) => Number(x).toFixed(2), f3 = (x) => Number(x).toFixed(3);
const sc = (s) => s.split("_")[0].toUpperCase();

// tiny markdown -> blocks (for embedding the decision log verbatim)
function mdBlocks(md) {
  const out = []; let para = [], bullets = [], code = null;
  const flushP = () => { if (para.length) { out.push({ t: "p", text: para.join(" ") }); para = []; } };
  const flushB = () => { if (bullets.length) { out.push({ t: "bullets", items: bullets }); bullets = []; } };
  for (const line of md.split("\n")) {
    if (code !== null) { if (line.startsWith("```")) { out.push({ t: "code", text: code.join("\n") }); code = null; } else code.push(line); continue; }
    if (line.startsWith("```")) { flushP(); flushB(); code = []; continue; }
    if (line.startsWith("# ")) continue;
    if (line.startsWith("## ")) { flushP(); flushB(); out.push({ t: "h2", text: line.slice(3).replace(/`/g, "") }); continue; }
    if (line.startsWith("### ")) { flushP(); flushB(); out.push({ t: "h3", text: line.slice(4).replace(/`/g, "") }); continue; }
    if (/^\s*- /.test(line)) { flushP(); bullets.push((line.startsWith("  ") ? "  " : "") + line.replace(/^\s*- /, "")); continue; }
    if (/^\s{2,}\S/.test(line) && bullets.length) { bullets[bullets.length - 1] += " " + line.trim(); continue; }
    if (line.trim() === "" || line.trim() === "---") { flushP(); flushB(); continue; }
    flushB(); para.push(line.trim());
  }
  flushP(); flushB();
  return out;
}

module.exports = () => {
  const evalDir = "results/evaluation/20261102T185926Z";
  const ev = J(`${evalDir}/summary.json`);
  const s2 = J("results/evaluation/20261102T191456Z_supplementary-16-rounds/summary.json").scenarios[0];
  const s5 = J("results/evaluation/20261102T191912Z_supplementary-after-F9/summary.json").scenarios[0];
  const v1 = J("results/evaluation/20261102T184027Z/summary.json");
  const calDir = latest("results/calibration/*/summary.json");
  const cal = J(calDir);
  const simFile = latest("results/similarity/*_benchmark.json");
  const sim = J(simFile);
  const repFile = latest("results/replication/*/summary.json");
  const rep = repFile ? J(repFile) : null;
  const tpchFile = latest("results/evaluation/*tpch*/summary.json");
  const tpch = tpchFile ? J(tpchFile) : null;
  const info = JSON.parse(fs.readFileSync(path.join(__dirname, "img", "chart_info.json"), "utf8"));
  const env = ev.environment;

  const byScen = Object.fromEntries(ev.scenarios.map((s) => [s.scenario, s]));
  const cands = (s, query) => s.incidents.filter((i) => !query || i.query === query).flatMap((i) => i.candidates);
  const verdictText = (c) => {
    if (!c.target) return `${c.description} → ${c.status}`;
    const e = c.target.effect, g = (c.guardrail || {}).queries || [];
    const coll = g.filter((x) => x.collateral_regression).map((x) => `${x.query} ${f2(x.ratio)}× [${f2(x.ci_low)}, ${f2(x.ci_high)}]`);
    return `\`${c.description}\` → **${c.label}**; target ${pct(e.ratio)} (CI ${f3(e.ci_low)}–${f3(e.ci_high)})` + (coll.length ? `; collateral: ${coll.join(", ")}` : g.length ? `; ${g.length} guardrail quer${g.length === 1 ? "y" : "ies"} clean` : "");
  };
  const scenRow = (s, note = "") => {
    const q = s.expected_query;
    const obs = q && s.observed_ratio[q] ? `${s.observed_ratio[q].toFixed(2)}×` : "—";
    const det = q === null ? (Object.keys(s.flagged_after_fault).length ? `flagged: ${Object.keys(s.flagged_after_fault).join(", ")}` : "no alarms")
      : s.detected ? `yes, interval ${s.detection_delay_rounds} after fault` : "no (within window)";
    const diag = s.diagnosis_rules.length ? s.diagnosis_rules.join(", ") + (s.diagnosis_correct === true ? " (correct)" : s.diagnosis_correct === false ? " (missed)" : "") : "—";
    const cs = (q ? cands(s, q) : cands(s)).map(verdictText).join("\n") || "—";
    return [sc(s.scenario) + note, obs, det, `${s.baseline_false_positives.length}`, diag, cs];
  };


  // ---------- worked example (all values from the meta DB, dumped by charts.py) ----------
  const W = JSON.parse(fs.readFileSync(path.join(__dirname, "img", "walkthrough.json"), "utf8"));
  const wi = W.incident, wp = W.plan, wc = W.candidates[0];
  const planRows = [];
  (function walk(n, d) {
    const loops = n["Actual Loops"] || 1;
    planRows.push([" ".repeat(d * 3) + n["Node Type"] + (n["Relation Name"] ? ` on ${n["Relation Name"]}` : ""),
      String(n["Plan Rows"]), String(n["Actual Rows"]), String(loops), n["Actual Total Time"] !== undefined ? n["Actual Total Time"].toFixed(1) : "",
      (n["Filter"] || n["Sort Key"] && "sort: " + n["Sort Key"].join(", ") || "") + (n["Rows Removed by Filter"] ? ` (removed ${n["Rows Removed by Filter"].toLocaleString("en-US")} per loop)` : "")]);
    (n.Plans || []).forEach((c) => walk(c, d + 1));
  })(wp.plan.Plan, 0);
  const tgt = wc.target.effect, gq = wc.guardrail.queries;
  const workedExample = [
    { t: "h2", text: "14.3 Worked example: one incident, every step, real values" },
    { t: "p", text: `Incident ${wi.id} (scenario ${wi.scenario}, after fix F-9). Every value below is read from the evidence database; this is exactly what CloudPulse stored.` },
    { t: "h3", text: "Step 1. Telemetry and detection" },
    { t: "code", text: `queryid ${wi.queryid}\n${wi.query_text}` , caption: "The statement as pg_stat_statements normalised it (constants became $n)" },
    { t: "bullets", items: [
      `Baseline: median of interval means **${wi.baseline_median_ms.toFixed(3)} ms**, MAD ${wi.baseline_mad_ms.toFixed(4)} ms (from up to 20 healthy intervals).`,
      `After the fault, intervals ${wi.evidence.anomalous_points.join(", ")} were anomalous (3 of the last 4 → regression declared). Current value: **${wi.current_ms.toFixed(1)} ms**, i.e. **${Math.round(wi.ratio).toLocaleString("en-US")}×** the baseline, robust z = ${Math.round(wi.evidence.z).toLocaleString("en-US")}.`,
      "A row was inserted into performance_incidents (the partial unique index guarantees it is the only open one for this query) and an investigate job was queued.",
    ] },
    { t: "h3", text: "Step 2. Plan capture on the target" },
    { t: "p", text: `EXPLAIN (ANALYZE, BUFFERS) was run in a READ ONLY transaction for 5 parameter values; the slowest used parameter ${JSON.stringify(wp.params)} and took **${wp.plan["Execution Time"].toFixed(1)} ms**. Plan fingerprint ${wp.fingerprint}; plan shape changed since the healthy baseline: **${wi.evidence.plan_changed_since_baseline ? "yes" : "no"}** (the healthy plan used the created_at index).` },
    { t: "table", headers: ["Node", "Est. rows", "Actual rows", "Loops", "Time ms", "Filter / detail"], widths: [1.6, 0.6, 0.7, 0.5, 0.6, 2.5], rows: planRows },
    { t: "h3", text: "Step 3. Findings" },
    { t: "bullets", items: wp.findings.map((f) => `**${f.kind}** on ${f.relation}: ` + (f.kind === "filtered_seq_scan"
      ? `${f.detail.rows_examined.toLocaleString("en-US")} rows examined, ${f.detail.rows_kept.toLocaleString("en-US")} kept (selectivity ${(f.detail.selectivity * 100).toFixed(2)}%), filter columns ${f.detail.filter_columns.join(", ")}`
      : `self time ${f.detail.self_time_ms} ms, stored share ${f.detail.share}. A share above 1 is the parallel-plan bug F-12, found while writing this guide and fixed since.`)) },
    { t: "h3", text: "Step 4. Diagnosis" },
    ...W.diagnoses.map((d) => ({ t: "callout", kind: "note", text: `[${d.rule}] ${d.hypothesis} Limitations: ${d.limitations}` })),
    { t: "h3", text: "Step 5. Candidate and HypoPG what-if" },
    { t: "bullets", items: [
      `Candidate: \`${wc.setup_sql[0]}\`.`,
      `HypoPG: the planner would use it; estimated cost of the target query ${Math.round(wc.hypopg.target_cost_before).toLocaleString("en-US")} → ${wc.hypopg.target_cost_after.toFixed(2)}.`,
      `Of up to ${wc.hypopg.details.params_checked} parameter values checked per tracked read, **${wc.hypopg.details.plans_changed_per_query.latest_orders_for_customer}** values of latest_orders_for_customer changed plan; the first ${wc.hypopg.changed_params.latest_orders_for_customer.length} (customers ${wc.hypopg.changed_params.latest_orders_for_customer.map((x) => x[0]).join(", ")}) were forwarded to the guardrail.`,
      `Writes on the table: ${wc.hypopg.writes_on_table.join(", ")}, added to the guardrail (index maintenance).`,
    ] },
    { t: "h3", text: "Step 6. Experiment" },
    { t: "p", text: `Twin databases were cloned from the lab base; arm B got the index. 15 blocks, ${W.n_raw_timings.toLocaleString("en-US")} timed executions stored (${path.basename(wc.run_dir)}).` },
    { t: "table", headers: ["Query", "Role", "Time ratio B/A", "CI", "Median A → B (µs)", "Outcome"], widths: [1.7, 0.7, 0.8, 1.1, 1.3, 1.2], rows: [
      ["recent_orders_feed", "target", f3(tgt.ratio), `95%: [${f3(tgt.ci_low)}, ${f3(tgt.ci_high)}]`, `${Math.round(tgt.median_us_a).toLocaleString("en-US")} → ${Math.round(tgt.median_us_b)}`, "≥ 5% faster: yes"],
      ...gq.map((g) => [g.query, "guardrail", f3(g.ratio), `${(g.confidence * 100).toFixed(1)}%: [${f2(g.ci_low)}, ${f2(g.ci_high)}]`, `${Math.round(g.median_us_a).toLocaleString("en-US")} → ${Math.round(g.median_us_b).toLocaleString("en-US")}`, g.collateral_regression ? "collateral regression (veto)" : g.ci_low > 1 ? "slower, below veto margin (warning)" : "ok"]),
    ] },
    { t: "callout", kind: "tip", text: "Look at latest_orders_for_customer: the median execution got slightly *faster*, yet the total time ratio is several times worse. Most customers benefit a little; a few heavy customers become dramatically slower and dominate the total. A median-based comparison would have approved this index. Using the total over the parameter mix, which is what the workload actually pays, is a deliberate design choice (D-010)." },
    { t: "h3", text: "Step 7. Verdict and outcome" },
    { t: "callout", kind: "key", text: `${wc.label}: ${wc.reason}` },
    { t: "p", text: `Incident status: **${wi.status}**: ${wi.outcome}. Nothing was applied to the target; a person reviews this evidence in the UI.` },
  ];
  const blocks = [];
  // ------------------------------------------------------------------ results
  blocks.push(
    { t: "h1", text: "14. Results" },
    { t: "h2", text: "14.1 Where the numbers come from" },
    { t: "table", headers: ["Item", "Value"], widths: [1.3, 4], rows: [
      ["Environment label", env.env_label],
      ["Host (main evaluation, calibration)", `${env.host.cpu_model}, ${env.host.cpu_count} vCPU, ${env.host.mem_total_gb} GB RAM, ${env.host.os}`],
      ["Host (replication, TPC-H)", rep ? `${rep.environment.host.cpu_model}, ${rep.environment.host.cpu_count} vCPU: the sandbox restarted on different hardware between sessions. Each results file records its own host.` : "—"],
      ["PostgreSQL", env.postgres.server_version + `; shared_buffers ${env.postgres.shared_buffers}, jit ${env.postgres.jit}, max_parallel_workers_per_gather ${env.postgres.max_parallel_workers_per_gather}`],
      ["Dataset", `e-commerce pack, scale "${ev.scale}" (100,000 customers, 2,000,000 orders), seed ${ev.seed}`],
      ["Protocol", `${ev.protocol.baseline_rounds || 14} pre-fault + ${ev.protocol.fault_rounds || 8} post-fault telemetry intervals per scenario; experiments: 15 blocks, ≤ 20 parameters per query, 1 warm-up round`],
      ["Main evaluation", evalDir],
      ["Supplementary runs", "…_supplementary-16-rounds (S2), …_supplementary-after-F9 (S5)"],
      ["Calibration / replication / similarity", `${path.dirname(calDir)} / ${repFile ? path.dirname(repFile) : "—"} / ${simFile}`],
    ] },
    { t: "h2", text: "14.2 Headline: the tempting index that is rejected" },
    { t: "p", text: `In S5 the target query (recent_orders_feed) became ${s5.observed_ratio.recent_orders_feed.toFixed(0)}× slower after the fault; it was detected at interval ${s5.detection_delay_rounds} after the fault and diagnosed as a selective sequential scan. The candidate \`CREATE INDEX ON orders (created_at)\` made the target ${pct(cands(s5, "recent_orders_feed")[0].target.effect.ratio)} faster, but:` },
    { t: "img", file: "chart_s5_customers.png", width: 600, caption: `Figure 14.1: Per-customer medians in the guardrail benchmark (${info.s5.run}). ${info.s5.n_faster} of ${info.s5.n_params} customers got faster, ${info.s5.n_slower15} got more than 1.5× slower.` },
    { t: "p", text: verdictText(cands(s5, "recent_orders_feed")[0]) + `. Weighted workload change: ${cands(s5, "recent_orders_feed")[0].guardrail.weighted_change_pct}% (informational).` },
    { t: "img", file: "chart_bootstrap.png", width: 600, caption: "Figure 14.2: How the guardrail interval for latest_orders_for_customer is built: 15 block ratios, then 10,000 bootstrap resamples. The whole interval lies far above the 1.10 veto line." },
    { t: "callout", kind: "tip", text: "This is the story to tell: the obvious fix is excellent for the query that regressed and terrible for a few big customers on another query. CloudPulse caught it because it benchmarks the parameter values where the plan changes, including the heaviest ones. Two earlier runs missed it, which is how F-7 and F-9 were discovered." },
    ...workedExample,
    { t: "h2", text: "14.4 All scenarios" },
    { t: "table", headers: ["Scenario", "Measured effect on its query", "Detected", "False alarms before fault", "Diagnosis", "Candidate → verdict"], widths: [0.7, 0.9, 1.2, 0.7, 1.1, 3.2], rows: [
      scenRow(byScen.s0_control), scenRow(byScen.s1_dropped_fk_index), scenRow(byScen.s2_stale_statistics), scenRow(s2, " (16-int.)"),
      scenRow(byScen.s3_work_mem_lowered), scenRow(byScen.s4_updated_at_index_dropped), scenRow(byScen.s5_created_at_index_dropped), scenRow(s5, " (after F-9)"),
      scenRow(byScen.s6_noisy_neighbor),
    ] },
    { t: "bullets", items: [
      `**False alarms:** 0 regressions declared in ${7 * 14 * 9 + 8 * 9} query-intervals with no fault active (7 scenarios × 14 pre-fault intervals × 9 queries, plus S0's 8 post-fault intervals).`,
      "**Detection:** S1, S4, S5 caught 2–3 intervals after the fault. S2 (1.83× on the template mean, because only one of its three parameter values regressed) was missed in 8 intervals and caught at interval 9 in a 16-interval run. S3's fault had no measurable effect (1.03×) and was correctly ignored.",
      "**Diagnosis:** the expected rule fired for every detected scenario.",
      "**S6 (contention):** the report queries were flagged; the `work_mem` candidate was REJECTED (no gain), and an index on `status` was SUPPORTED. It genuinely speeds that query in isolation but does not address the actual cause. A SUPPORTED verdict is about the candidate, not a root-cause claim.",
    ] },
    { t: "img", file: "chart_detection.png", width: 600, caption: "Figure 14.4: Interval means relative to each query's own baseline median; circles mark intervals counted as anomalous." },
    { t: "h2", text: "14.5 A/A calibration: can the method be trusted?" },
    { t: "p", text: `${cal.repeats} repeats × ${cal.queries.length} queries with identical arms and fresh twins (${calDir}). False SUPPORTED: **${cal.false_supported}/${cal.n_estimates}**. False collateral flags: **${cal.false_collateral}/${cal.n_estimates}**. 95% CI contains 1.0: **${cal.ci_contains_1}/${cal.n_estimates}** (nominal 95%). Largest A/A difference: ${cal.max_abs_change_pct}% (on the UPDATE). Verdict counts: ${Object.entries(cal.verdict_counts).map(([k, v]) => `${k} ${v}`).join(", ")}.` },
    { t: "img", file: "chart_calibration.png", width: 600, caption: "Figure 14.5: Every A/A estimate with its 95% CI. None reaches the SUPPORTED or veto lines with its whole interval." },
  );
  if (rep) {
    const rows = rep.candidates.map((c) => {
      const st = c.stability || {};
      return [sc(c.scenario), `\`${c.candidate}\``, `${c.original_label} (${f3(c.original_ratio)})`,
        c.replications.map((r) => `${r.label[0]} ${f3(r.ratio)}${r.collateral.length ? " vetoed by " + r.collateral.join(",") : ""}`).join("\n"),
        `**${st.consolidated || "?"}**`];
    });
    blocks.push(
      { t: "h2", text: "14.6 Replication: are single-run verdicts stable?" },
      { t: "p", text: `Each tested candidate was re-run ${rep.repeats} more times with fresh twin databases and new shuffle seeds (${path.dirname(repFile)}). The consolidated verdict is the common label if all runs agree, otherwise UNSTABLE (D-023). In the table, S = SUPPORTED, R = REJECTED, I = INCONCLUSIVE; "vetoed by" names the query with a collateral regression.` },
      { t: "table", headers: ["Scen.", "Candidate", "Original (target ratio)", "Replications (target ratio)", "Consolidated"], widths: [0.5, 2.2, 1.2, 2, 1], rows },
      { t: "img", file: "chart_replication.png", width: 600, caption: "Figure 14.6: Target time ratio per run; colour is the verdict of that run." },
      { t: "callout", kind: "key", text: "Verdicts far from a decision margin replicate perfectly. Verdicts that depend on an effect close to a margin (a ~10–25% write slowdown against a 10% veto) flip between runs. Report those as UNSTABLE and let a human decide, or pool more runs. Never present one run's verdict as settled." },
    );
  }
  if (tpch) {
    const rows = tpch.scenarios.map((s) => scenRow(s));
    blocks.push(
      { t: "h2", text: "14.7 External validation on TPC-H" },
      { t: "p", text: `Same engine, same rules, same thresholds, applied to the TPC-H benchmark (${tpch.scale}, data from \`tpchgen-cli\`; ${path.dirname(tpchFile)}). Nothing was tuned for TPC-H.` },
      { t: "table", headers: ["Scenario", "Measured effect on its query", "Detected", "False alarms before fault", "Diagnosis", "Candidate → verdict"], widths: [0.7, 0.9, 1.2, 0.7, 1.1, 3.2], rows },
      { t: "img", file: "chart_tpch.png", width: 600, caption: "Figure 14.7: Measured effect of each TPC-H fault on its query." },
    );
  }
  blocks.push(
    { t: "h2", text: `${tpch ? "14.8" : "14.7"} Similarity search` },
    { t: "p", text: `On 30 hand-written queries in 6 structural families: recall@1 ${sim[0].recall_at_k_structural} vs ${sim[0].recall_at_k_text_jaccard} for a text baseline (token Jaccard); recall@4 ${sim[2].recall_at_k_structural} vs ${sim[2].recall_at_k_text_jaccard}. The families were written by the same author as the features, so this shows the features behave as designed, not that they generalise.` },
    { t: "h2", text: `${tpch ? "14.9" : "14.8"} The superseded first run` },
    { t: "p", text: `The first full evaluation (20261102T184027Z) is kept for the record. It differed because of two bugs found in it: F-7 (S5 guardrail sampling gap → S5 SUPPORTED) and F-8 (queries mapped to an earlier scenario's database → fake "false positives" in S5 and S6). It also measured S4's write slowdown at +13% (SUPPORTED with a warning), where the corrected run measured +23% (REJECTED). That disagreement is what motivated replication.` },
  );

  // ------------------------------------------------------------------ mistakes
  const dec = fs.readFileSync(path.join(REPO, "docs/decisions.md"), "utf8");
  const fPart = dec.slice(dec.indexOf("## Failed hypotheses and lessons"));
  blocks.push(
    { t: "h1", text: "15. What went wrong, and what it taught" },
    { t: "p", text: "Every failed hypothesis, bug and wrong result found during the project, verbatim from `docs/decisions.md`. These are the most valuable interview stories: each one shows how a problem was noticed, diagnosed from evidence, fixed, and documented without rewriting history." },
    ...mdBlocks(fPart).filter((b) => b.t !== "h2"),
  );

  // ------------------------------------------------------------------ testing
  const testCount = execSync(`cd ${REPO} && python3 -m pytest --collect-only -q 2>/dev/null | tail -1`, { encoding: "utf8" }).trim();
  blocks.push(
    { t: "h1", text: "16. Testing, CI and reproducibility" },
    { t: "p", text: `Test suite: ${testCount}. Unit tests run without a database; tests marked \`db\` use the three servers and their own scratch databases (cp_test_*), never the real ones.` },
    { t: "table", headers: ["File", "What it proves"], widths: [1.4, 4], rows: [
      ["tests/test_stats.py", "Effect recovery; block drift cancels; CI coverage ≥ 88% by simulation; A/A false-SUPPORTED ≤ 3%; decision rule cases; timeouts; collateral detection"],
      ["tests/test_detector.py", "No flags on stable noise; exact delay on a step; spike ignored; small change ignored; flat baseline; invalid and empty intervals; config respected"],
      ["tests/test_plans.py", "Golden files (real EXPLAIN JSON from PostgreSQL): parsing, selectivity, misestimate only on scans, spill detected once, index walk, healthy plan, estimate-only plan, non-negative self time, fingerprint stability and sensitivity, filter-column extraction"],
      ["tests/test_rules.py", "Each rule on golden plans; existing index not re-proposed; ANALYZE only when stale; scoped work_mem capped; composite index for the walk; cause_unclear; DDL format"],
      ["tests/test_harness_units.py", "Arm alternation; deterministic permutations; base-key sensitivity; spec parsing; safety guard; identifier guard; guardrail veto/pass/warning; union sampler; plan-changing params in guardrail; replication consolidation"],
      ["tests/test_similarity.py", "Structural features; arithmetic is not a predicate; surface changes do not change the vector; unit length; beats text baseline"],
      ["tests/test_db_integration.py", "Deterministic generation and skew; harness records every execution and cleans up; result-changing candidate rejected; target refused; collector deltas and reset handling; SKIP LOCKED gives distinct jobs; lease expiry, reclaim, max attempts; requeue on failure; schema CHECKs; dbid scoping (F-8); heavy hitters; TPC-H pack builds and samples deterministically"],
      ["tests/test_api.py", "Health; pages render real plan, candidate, guardrail and percentage; JSON API and 404; investigate endpoint enqueues a job"],
    ] },
    { t: "p", text: "**CI** (GitHub Actions, every push): build the PostgreSQL 17 + HypoPG + pgvector image, start the three servers with `docker compose up -d --build --wait`, install, `ruff check`, `pytest -q`, run the similarity benchmark. **Reproduce** everything on your machine with `./scripts/reproduce.sh` (labels results `official-<hostname>`; `--quick` for a short run)." },
  );

  // ------------------------------------------------------------------ limitations
  blocks.push(
    { t: "h1", text: "17. Limitations and future work" },
    { t: "bullets", items: [
      "**Hardware and version:** results are from a noisy 2-vCPU VM running PostgreSQL 16; official numbers should come from your machine on PostgreSQL 17.",
      "**Self-designed scenarios:** the e-commerce faults and the rules share an author. TPC-H reduces but does not remove this (the faults on TPC-H were still chosen by the same author).",
      "**Run-to-run noise:** single-run CIs are optimistic (90% A/A coverage); verdicts near a margin flip between runs (S1, S4). Use replication and treat UNSTABLE as \"needs a human\".",
      "**Detector on means:** a regression that affects only some parameter values is diluted (S2). Fix: detect per parameter group or on per-call latency samples.",
      "**Lab state:** rebuilt from generator + SQL; real deployments need snapshot restore (and `ANALYZE`, since statistics are not dumped before PostgreSQL 18).",
      "**Not diagnosable:** locks, I/O or CPU saturation, checkpoints, bloat, generic-plan flips, network.",
      "**Candidate space:** no extended statistics, partial or expression indexes, or query rewrites yet.",
      "**Guardrail weighting:** the veto ignores frequency; heavy-hitter parameters are over-represented on purpose (the conservative question).",
    ] },
    { t: "p", text: "**Natural next steps:** pooled analysis across replications; per-parameter-group detection (fixes S2); extended statistics as a candidate for `estimation_limit`; generic-plan regressions as a scenario; snapshot-based lab provisioning; auto_explain ingestion for plans captured at the moment of slowness." },
  );

  // ------------------------------------------------------------------ interview
  blocks.push(
    { t: "h1", text: "18. Interview preparation" },
    { t: "h2", text: "18.1 The 30-second pitch" },
    { t: "p", text: "\"CloudPulse is a PostgreSQL performance tool I built that treats every index or tuning suggestion as a hypothesis. When a query regresses, it captures the real plan, proposes a fix, and benchmarks it on two identical copies of the database with alternating trials and bootstrap confidence intervals. Crucially, it checks the rest of the workload too. In one case the obvious index made the regressed query 99.7% faster but made a customer-lookup query 5.7 times slower for heavy customers, so the tool rejected it.\"" },
    { t: "h2", text: "18.2 The 2-minute version" },
    { t: "steps", ref: "steps20", items: [
      "**Problem:** tools say \"add an index\"; nobody checks whether it helps, how sure we are, or what it breaks.",
      "**Detection:** pg_stat_statements is cumulative, so I snapshot and difference it, handle resets and restarts, and compare each query with its own history using median/MAD with a sustain rule.",
      "**Diagnosis:** I parse EXPLAIN ANALYZE JSON into a tree, compute q-errors and spills, and explainable rules produce hypotheses plus candidates.",
      "**Experiment:** template-copied twin databases so layout and statistics are identical; alternating blocks; a result-equivalence check; a paired block bootstrap; a decision rule fixed in advance.",
      "**Guardrail:** HypoPG finds which other queries, and which parameter values, would change plan; those plus writes are benchmarked with Bonferroni correction and a 10% veto.",
      "**Honesty:** A/A calibration, replication, TPC-H validation, and twelve documented mistakes, including two runs where the headline result was wrong and how I found out.",
    ] },
    { t: "h2", text: "18.3 How to demo it" },
    { t: "steps", ref: "steps21", items: [
      "`docker compose up -d --build --wait`, then `pip install -e .`",
      "`cloudpulse bench experiments/ab_created_at_index_trap.json`: shows REJECTED with the collateral query, live.",
      "`cloudpulse evaluate --scale small --scenario s5_created_at_index_dropped` then `cloudpulse serve` and open the incident page: walk through the five sections.",
      "Open `results/runs/<run>/raw.csv` to show that every number is backed by raw data.",
    ] },
    { t: "h2", text: "18.4 Question drill" },
    { t: "qa", items: [
      ["Why not just run EXPLAIN on the slow query and add the index it suggests?", "EXPLAIN shows estimates, and the planner's estimates are what went wrong. An index's benefit, its cost to writes, and its effect on other queries' plans can only be known by measuring. HypoPG and EXPLAIN are filters, not proof."],
      ["Why are pg_stat_statements counters cumulative and what does it force you to do?", "Cheap running sums in shared memory. You must snapshot and difference them, and detect resets (stats_reset), restarts (postmaster start time) and evictions (calls going backwards) so you never compute a fake negative delta."],
      ["Why can't you get p95 latency per interval from pg_stat_statements?", "Only sums can be differenced. min/max/stddev are cumulative since reset. So the detector uses interval means, and its weakness is parameter mix (S2)."],
      ["Why median/MAD instead of mean and standard deviation?", "One extreme interval inflates mean and standard deviation, hiding later anomalies. Median and MAD resist a minority of outliers. Floors stop a flat baseline from producing infinite z-scores."],
      ["Why require 3 of the last 4 intervals?", "A single slow interval is usually noise (a checkpoint, a cold cache). A sustained shift is a regression. The cost is detection delay (2–3 intervals here)."],
      ["Why can a later EXPLAIN ANALYZE differ from the slow execution?", "Different parameters, cache state, statistics (autovacuum may have analysed), or concurrency. CloudPulse tries several parameters, keeps the slowest, and records whether the plan shape changed since a healthy baseline."],
      ["What is q-error and why does it matter?", "max(estimate/actual, actual/estimate). Join methods and orders are chosen from estimates; estimating 1 row instead of 300,000 picks a nested loop with 300,000 probes (S2)."],
      ["How does PostgreSQL choose between a seq scan and an index scan?", "Estimated cost: estimated rows × per-row CPU costs plus pages × page costs (sequential vs random), adjusted by the correlation between index order and physical order. Wrong row estimates mean wrong choices."],
      ["Why can adding an index make another query slower?", "It changes the planner's options. In S5 the planner walks the created_at index for ORDER BY … LIMIT, assuming matching rows are spread evenly; for heavy customers with only old orders it reads most of the table. Indexes also add work to every insert and non-HOT update."],
      ["What is a HOT update, and how did the project show its cost?", "An update that changes no indexed column and fits on the same page writes no index entries. Indexing updated_at turned touch_order from 100% HOT to 0% HOT; the guardrail measured roughly +13–23% on that update."],
      ["Why two databases instead of BEGIN; CREATE INDEX; ROLLBACK?", "A plain CREATE INDEX holds a SHARE lock blocking writes for the whole benchmark; the build warms the cache for one side; CONCURRENTLY can't run in a transaction. Twins from a template are identical in layout and statistics and can be alternated."],
      ["Why alternate AB, BA instead of A then B?", "Time-dependent effects (cache warming, CPU frequency, bloat from writes) would systematically favour one arm. Alternating spreads them over both, and pairing within blocks cancels them."],
      ["Why bootstrap over blocks rather than executions?", "Executions within a block are correlated; resampling them as if independent understates uncertainty. Blocks are the paired, more independent unit."],
      ["Why log ratios?", "Ratios are multiplicative: 2× faster and 2× slower are symmetric in log space. Averaging raw ratios is biased."],
      ["Why a confidence interval rather than a p-value?", "The decision depends on effect size relative to a practical margin (5%), not only on whether the effect differs from zero."],
      ["Why INCONCLUSIVE?", "Forcing yes/no on noisy data creates confident wrong answers. A tool that recommends schema changes must be able to say \"not enough evidence\"."],
      ["What does Bonferroni do here and what does it cost?", "Checking m guardrail queries at 95% each gives ~m×5% chance of a false alarm; using 1 − 0.05/m per query keeps the family rate near 5%. The cost is power: small real regressions are harder to flag."],
      ["Your A/A intervals covered 1.0 only 90% of the time. Is the method broken?", "Single-run intervals are optimistic because run-to-run variance exceeds within-run variance. The 5%/10% margins absorbed it (zero false verdicts). The fix is replication or margins calibrated from A/A data, which is what the replication step does."],
      ["A verdict flipped between runs. What do you conclude?", "The true effect is near a decision margin (S4: a write slowdown of ~10–25% against a 10% veto). Report UNSTABLE and let a human weigh it; do not pick the run you like."],
      ["What does HypoPG tell you and what doesn't it?", "Whether the planner would use a hypothetical index and which plans would change, cheaply. Not runtime, cache effects, or write cost. It's a pre-filter and a scoping tool."],
      ["How did the guardrail miss the trap at first?", "It benchmarked a random sample of customers; only a few heavy customers trigger the bad plan. F-7 added the exact plan-changing parameter values; F-9 added the heaviest values to the what-if check, because plan flips concentrate on most-common values."],
      ["Why work_mem only per session?", "work_mem is per operation per session; raising it globally multiplies across concurrent sessions and operations and can exhaust memory. Also, removing a spill may not help at all (F-1, S6)."],
      ["How does the job queue survive a worker crash?", "Claim with FOR UPDATE SKIP LOCKED and a 120 s lease renewed every 30 s. A crashed worker stops renewing; the lease expires; another worker reclaims the job (attempts+1). Jobs are idempotent. After max_attempts the job is failed with its last error."],
      ["Why PostgreSQL as a queue instead of Kafka/Redis?", "A handful of minute-long jobs needing durability and transactional consistency with the evidence. PostgreSQL already provides that; another system adds operations without a measured need."],
      ["How do you prevent duplicate incidents?", "A partial unique index on performance_incidents(query_id) WHERE status IN ('open','investigating'). The second insert fails and is ignored."],
      ["How did you map pg_stat_statements entries to your templates?", "Not by text (constants become $n). EXPLAIN (VERBOSE) prints the same Query Identifier pg_stat_statements uses. Integers are always sent as int8, because queryid includes parameter types (F-4)."],
      ["What bug produced fake false positives?", "F-8: the helper looked up a template's query row without scoping to the current database, so an earlier scenario's regression was re-reported. Fix: filter by dbid; add a two-database test."],
      ["Why pgvector with exact search and hand-designed features?", "Explainable, reproducible dimensions; tens to thousands of rows make a sequential scan instant and exact. ANN indexes trade recall for speed that isn't needed."],
      ["What can CloudPulse not do?", "Diagnose locks, saturation, checkpoints, bloat, generic-plan flips; propose rewrites or extended statistics; know production traffic shape beyond the tracked workload; apply anything automatically, by design."],
      ["How do you know your results aren't just noise?", "A/A calibration on the same machine; decision margins; result-equivalence checks; replication with consolidated verdicts; raw data for every number; TPC-H as a workload not designed around the rules."],
      ["What would you do with another month?", "Per-parameter-group detection; pooled multi-run analysis; extended statistics candidates; snapshot-based lab provisioning; run on real production-like traces."],
    ] },
  );

  // ------------------------------------------------------------------ glossary
  blocks.push(
    { t: "h1", text: "19. Glossary" },
    { t: "table", headers: ["Term", "Meaning"], widths: [1.2, 4], rows: [
      ["A/A test", "Experiment with two identical arms; measures the method's own noise"],
      ["Arm", "One side of an experiment (A = unchanged, B = with candidate)"],
      ["Block", "One pass of the full parameter list on both arms; the unit of pairing"],
      ["Bonferroni", "Per-test confidence 1 − α/m to control false alarms across m tests"],
      ["Candidate", "A concrete proposed change (index, ANALYZE, session setting)"],
      ["Cardinality", "Number of rows a plan step produces"],
      ["Collateral regression", "Another query confidently slower by more than 10% under the candidate"],
      ["Fingerprint (plan)", "Hash of a plan's shape: node types, tables, indexes, join types"],
      ["Guardrail", "The check of other tracked queries for collateral regressions"],
      ["Heavy hitter", "One of the most frequent parameter values (e.g. biggest customers)"],
      ["HOT update", "Heap-only tuple update: no index entries written"],
      ["HypoPG", "Extension for hypothetical indexes visible only to the planner"],
      ["Incident", "A detected regression of one query"],
      ["Lease", "Time-limited ownership of a job by a worker"],
      ["MAD", "Median absolute deviation, a robust measure of spread"],
      ["MCV list", "Most-common values and their frequencies in planner statistics"],
      ["Pack", "Schema + data + workload + baseline + faults for one application"],
      ["q-error", "max(estimated/actual, actual/estimated) rows"],
      ["Spill", "A sort/hash that exceeded work_mem and wrote temp files"],
      ["Twin databases", "Two file-level copies of the same base database"],
      ["UNSTABLE", "Consolidated verdict when replications disagree"],
    ] },
  );

  // ------------------------------------------------------------------ appendices
  const files = execSync(`cd ${REPO} && git ls-files | grep -v '^results/' | grep -v '^tests/golden/' | xargs wc -l | grep -v total`, { encoding: "utf8" })
    .trim().split("\n").map((l) => l.trim().split(/\s+/)).map(([n, f]) => [f, n]);
  blocks.push(
    { t: "h1", text: "Appendix A. Decision log (verbatim)" },
    ...mdBlocks(dec.slice(0, dec.indexOf("## Failed hypotheses and lessons"))),
    { t: "h1", text: "Appendix B. Configuration reference" },
    { t: "table", headers: ["Setting", "Value", "Where"], widths: [2.2, 1.2, 2], rows: [
      ["Target improvement margin", "5% (CI upper < 0.95)", "workload/guardrail.py TARGET_MARGIN"],
      ["Collateral veto margin", "10% (adjusted CI lower > 1.10)", "guardrail.py HARM_MARGIN"],
      ["Guardrail confidence", "1 − 0.05/m", "guardrail.evaluate"],
      ["Minimum blocks for a verdict", "10", "verifier.decide_target"],
      ["Bootstrap resamples", "10,000", "verifier.estimate"],
      ["Pipeline protocol", "15 blocks, 1 warm-up, ≤ 20 params/query", "pipeline.py"],
      ["Statement timeout in lab", "60 s", "bench/spec.py Protocol"],
      ["Plan capture", "5 parameters, READ ONLY, 60 s timeout", "pipeline.py"],
      ["Detector", "window 20, min baseline 8, z > 4, ≥ 1.5×, floors 5% / 0.05 ms, 3 of 4", "telemetry/detector.py"],
      ["Selective scan rule", "≥ 10,000 rows examined, ≤ 5% kept", "diagnosis/rules.py"],
      ["Stale statistics rule", "q-error ≥ 10, ≥ 1000 rows, ≥ 5% rows modified", "plans/features.py, rules.py"],
      ["Index-walk finding", "≥ 10,000 examined, < 1% kept", "plans/features.py"],
      ["work_mem cap", "256 MB, session only", "rules.py"],
      ["HypoPG parameters checked", "up to 60 (sample + 20 heavy hitters); ≤ 10 changed values forwarded", "diagnosis/hypo.py, bench/params.py"],
      ["Job lease / heartbeat / attempts", "120 s / 30 s / 3", "jobs/queue.py, worker.py"],
      ["Server settings (compose)", "shared_buffers 256MB, jit off, track_io_timing on, pg_stat_statements max 5000, 2 CPU / 2 GB", "docker-compose.yml"],
    ] },
    { t: "h1", text: "Appendix C. Repository file map" },
    { t: "table", headers: ["File", "Lines"], widths: [5, 0.6], rows: files },
    { t: "h1", text: "Appendix D. Evidence schema (full SQL)" },
    { t: "code", text: fileText("src/cloudpulse/meta/schema.sql") },
  );
  return blocks;
};
