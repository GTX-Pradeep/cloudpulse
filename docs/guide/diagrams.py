"""Architecture diagrams for the CloudPulse guide (Graphviz -> PNG)."""
import subprocess
from pathlib import Path

OUT = Path(__file__).parent / "img"
OUT.mkdir(exist_ok=True)

FONT = 'fontname="DejaVu Sans"'
BLUE, GREEN, ORANGE, PURPLE, GREY, RED = "#2f5bd3", "#1f7a4d", "#b8650a", "#6b3fa0", "#5f6670", "#b4232c"


def render(name: str, dot: str) -> None:
    src = OUT / f"{name}.dot"
    src.write_text(dot)
    subprocess.run(["dot", "-Tpng", "-Gdpi=200", str(src), "-o", str(OUT / f"{name}.png")], check=True)


def node(n, label, color, shape="box", fill="#ffffff", style="rounded,filled"):
    return f'{n} [label="{label}", shape={shape}, style="{style}", color="{color}", fillcolor="{fill}", penwidth=1.6];'


# 1. system architecture -----------------------------------------------------------
render("architecture", f"""
digraph G {{
  rankdir=TB; splines=true; nodesep=0.3; ranksep=0.45; newrank=true; {FONT}; node [{FONT}, fontsize=11]; edge [{FONT}, fontsize=9, color="{GREY}"];
  subgraph cluster_target {{ label="target-db  (monitored; never experimented on)"; style="rounded,dashed"; color="{BLUE}"; fontcolor="{BLUE}";
    {node("app", "Application / workload\\n(tracked query templates)", BLUE, fill="#eef2fd")}
    {node("pgss", "pg_stat_statements\\n(cumulative counters)", BLUE, shape="cylinder", style="filled", fill="#eef2fd")}
    app -> pgss [label="executes"];
  }}
  subgraph cluster_engine {{ label="CloudPulse engine (Python)"; style="rounded"; color="{GREY}"; fontcolor="{GREY}";
    {node("coll", "Collector\\nsnapshot + deltas", GREY)}
    {node("det", "Detector\\nmedian / MAD, sustain 3 of 4", GREY)}
    {node("worker", "Worker\\n(claims jobs)", GREY)}
    {node("cap", "Plan capture\\nEXPLAIN ANALYZE, read-only", GREY)}
    {node("ana", "Plan analyzer\\ntree, q-error, spills", GREY)}
    {node("rules", "Diagnosis rules\\nhypotheses + candidates", GREY)}
    {node("hypo", "HypoPG what-if\\nused? which plans change?", PURPLE, fill="#f4effa")}
    {node("harn", "A/B harness\\ntwins, AB/BA blocks", ORANGE, fill="#fdf3e6")}
    {node("stats", "Verifier + guardrail\\nblock bootstrap, Bonferroni", ORANGE, fill="#fdf3e6")}
  }}
  subgraph cluster_lab {{ label="lab-db  (experiments)"; style="rounded,dashed"; color="{ORANGE}"; fontcolor="{ORANGE}";
    {node("base", "base database\\n(state of target)", ORANGE, shape="cylinder", style="filled", fill="#fdf3e6")}
    {node("ta", "twin A\\n(unchanged)", ORANGE, shape="cylinder", style="filled", fill="#fdf3e6")}
    {node("tb", "twin B\\n(+ candidate)", ORANGE, shape="cylinder", style="filled", fill="#fdf3e6")}
    base -> ta [label="TEMPLATE copy"]; base -> tb [label="TEMPLATE copy"];
  }}
  subgraph cluster_meta {{ label="meta-db  (evidence store)"; style="rounded,dashed"; color="{GREEN}"; fontcolor="{GREEN}";
    {node("ev", "snapshots, incidents, plans,\\ndiagnoses, candidates, experiments,\\nraw runs, verdicts, embeddings", GREEN, shape="cylinder", style="filled", fill="#e9f5ee")}
    {node("jobs", "jobs queue\\n(SKIP LOCKED + lease)", GREEN, shape="cylinder", style="filled", fill="#e9f5ee")}
  }}
  {node("ui", "FastAPI\\nweb UI + JSON API", GREEN, fill="#e9f5ee")}
  pgss -> coll [label="read every interval"];
  coll -> ev; ev -> det; det -> jobs [label="incident -> investigate job"];
  jobs -> worker; worker -> cap; cap -> app [style=dashed, label="on target"]; cap -> ana -> rules -> hypo;
  hypo -> base [style=dashed]; hypo -> harn [label="candidate worth testing"];
  harn -> ta; harn -> tb; harn -> stats [label="raw timings"]; stats -> ev [label="verdict + evidence"];
  ev -> ui;
}}
""")

# 2. incident lifecycle (sequence-like, top to bottom) -------------------------------
render("lifecycle", f"""
digraph G {{
  rankdir=TB; nodesep=0.25; ranksep=0.28; {FONT}; node [{FONT}, fontsize=11, shape=box, style="rounded,filled", fillcolor="#ffffff", penwidth=1.4]; edge [color="{GREY}", arrowsize=0.7];
  s1 [label="1. Workload runs on target-db; pg_stat_statements accumulates counters", color="{BLUE}", fillcolor="#eef2fd"];
  s2 [label="2. Collector snapshots counters -> per-interval mean latency (invalid if reset/restart/eviction)", color="{GREY}"];
  s3 [label="3. Detector: is this query unusually slow for ITSELF? (z > 4 AND >= 1.5x, 3 of last 4 intervals)", color="{GREY}"];
  s4 [label="4. Incident row created (one open incident per query, enforced by a partial unique index)\\n+ 'investigate' job enqueued", color="{GREEN}", fillcolor="#e9f5ee"];
  s5 [label="5. Worker claims job (FOR UPDATE SKIP LOCKED, 120 s lease, heartbeat every 30 s)", color="{GREEN}", fillcolor="#e9f5ee"];
  s6 [label="6. Capture plan on target: EXPLAIN (ANALYZE, BUFFERS) for 5 parameter values, READ ONLY, 60 s timeout; keep slowest", color="{BLUE}", fillcolor="#eef2fd"];
  s7 [label="7. Plan analyzer -> findings (filtered seq scan, misestimate, spill, ordered-index walk, nested loop, hotspot)", color="{GREY}"];
  s8 [label="8. Diagnosis rules -> hypotheses (with evidence + limitations) -> candidates (index / ANALYZE / session work_mem)", color="{GREY}"];
  s9 [label="9. HypoPG on lab base: would the planner use the index? which other queries (and which parameter values) change plan?", color="{PURPLE}", fillcolor="#f4effa"];
  s10 [label="10. 'experiment' job per surviving candidate", color="{GREEN}", fillcolor="#e9f5ee"];
  s11 [label="11. Harness: base -> twin A + twin B(+candidate); warm-up + result-equivalence check; 15 blocks AB/BA", color="{ORANGE}", fillcolor="#fdf3e6"];
  s12 [label="12. Verifier: log ratio of block totals, paired block bootstrap 95% CI, margin 5%", color="{ORANGE}", fillcolor="#fdf3e6"];
  s13 [label="13. Guardrail: each affected query at 1 - 0.05/m confidence; veto if CI entirely above +10%", color="{ORANGE}", fillcolor="#fdf3e6"];
  s14 [label="14. Verdict SUPPORTED / REJECTED / INCONCLUSIVE + reason; raw timings COPYed to meta; incident closed", color="{GREEN}", fillcolor="#e9f5ee"];
  s15 [label="15. Human reviews evidence in the UI. Nothing is ever applied to target automatically.", color="{RED}", fillcolor="#fbe9ea"];
  s1->s2->s3->s4->s5->s6->s7->s8->s9->s10->s11->s12->s13->s14->s15;
}}
""")

# 3. the A/B harness ------------------------------------------------------------------
render("harness", f"""
digraph G {{
  rankdir=TB; nodesep=0.3; ranksep=0.45; {FONT}; node [{FONT}, fontsize=11]; edge [color="{GREY}", fontsize=9, {FONT}];
  gen [label="deterministic data\\n(generator + seed)\\n+ state SQL\\n(baseline + fault)", shape=note, color="{GREY}"];
  base [label="cp_base_<hash>\\nVACUUM FREEZE, ANALYZE\\nautovacuum off", shape=cylinder, style=filled, fillcolor="#fdf3e6", color="{ORANGE}"];
  a [label="cp_exp_a\\narm A: no change", shape=cylinder, style=filled, fillcolor="#eef2fd", color="{BLUE}"];
  b [label="cp_exp_b\\narm B: CREATE INDEX ...\\nor ANALYZE / SET work_mem", shape=cylinder, style=filled, fillcolor="#e9f5ee", color="{GREEN}"];
  gen -> base [label="build once, reuse"];
  base -> a [label="CREATE DATABASE\\nTEMPLATE (file copy)"];
  base -> b [label="CREATE DATABASE\\nTEMPLATE (file copy)"];
  subgraph cluster_blocks {{ label="measurement: same shuffled parameter list on both arms in every block"; style=rounded; color="{GREY}"; fontcolor="{GREY}";
    b0 [label="block 0\\nA then B", shape=box, style=rounded];
    b1 [label="block 1\\nB then A", shape=box, style=rounded];
    b2 [label="block 2\\nA then B", shape=box, style=rounded];
    bn [label="... 15 blocks", shape=plaintext];
    {{ rank=same; b0; b1; b2; bn; }}
    b0 -> b1 -> b2 -> bn [arrowsize=0.6];
  }}
  a -> b0 [style=dashed]; b -> b0 [style=dashed];
  out [label="raw.csv (every execution)\\nmeta.json (env, settings, dataset)\\nplans.jsonl, verdict.json", shape=note, color="{GREY}"];
  bn -> out;
}}
""")

# 4. evidence schema (ER) -------------------------------------------------------------
def tbl(name, cols, color):
    rows = "".join(f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="9">{c}</FONT></TD></TR>' for c in cols)
    return (f'{name} [shape=plaintext, label=<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="3" COLOR="{color}">'
            f'<TR><TD BGCOLOR="{color}"><FONT COLOR="white"><B>{name}</B></FONT></TD></TR>{rows}</TABLE>>];')

render("schema", f"""
digraph G {{
  rankdir=TB; nodesep=0.3; ranksep=0.5; {FONT}; node [{FONT}]; edge [color="{GREY}", arrowhead=crow, arrowtail=none, dir=both, arrowsize=0.7];
  {tbl("queries", ["id PK", "queryid, dbid, userid, toplevel (UNIQUE)", "query_text, template_name"], BLUE)}
  {tbl("stat_snapshots", ["id PK", "taken_at, stats_reset", "dealloc, postmaster_start"], BLUE)}
  {tbl("query_stats_snapshots", ["snapshot_id FK, query_id FK (PK)", "calls, total_exec_time_ms", "rows, blks hit/read, temp written"], BLUE)}
  {tbl("performance_incidents", ["id PK, query_id FK", "baseline median/MAD, current, ratio", "evidence jsonb, status CHECK", "partial UNIQUE: one open per query"], GREEN)}
  {tbl("execution_plans", ["id PK, query_id FK, incident_id FK", "source CHECK, params", "fingerprint, plan jsonb, findings"], GREEN)}
  {tbl("diagnoses", ["id PK, incident_id FK", "rule, hypothesis", "evidence, limitations"], GREEN)}
  {tbl("candidates", ["id PK, incident_id FK, diagnosis_id FK", "kind CHECK (index|analyze|setting)", "setup_sql, session, hypopg", "status CHECK"], PURPLE)}
  {tbl("experiments", ["id PK, candidate_id FK", "protocol, environment", "run_dir, status, error"], ORANGE)}
  {tbl("experiment_runs", ["(experiment, query, block, arm, seq) PK", "latency_us, status, rows"], ORANGE)}
  {tbl("verdicts", ["id PK, experiment_id FK UNIQUE", "label CHECK", "reason, target, guardrail"], ORANGE)}
  {tbl("jobs", ["id PK, kind CHECK, payload", "status, attempts, max_attempts", "locked_by, lease_until, last_error"], GREY)}
  {tbl("query_embeddings", ["incident_id PK/FK", "repr_version, features", "embedding vector(32)"], GREY)}
  queries -> query_stats_snapshots; stat_snapshots -> query_stats_snapshots;
  queries -> performance_incidents; queries -> execution_plans; performance_incidents -> execution_plans;
  performance_incidents -> diagnoses; performance_incidents -> candidates; diagnoses -> candidates;
  candidates -> experiments; experiments -> experiment_runs; experiments -> verdicts [arrowhead=tee];
  performance_incidents -> query_embeddings [arrowhead=tee];
  view [label="VIEW query_intervals\\n(delta of consecutive snapshots,\\ninvalid on reset / restart / eviction)", shape=note, color="{BLUE}"];
  query_stats_snapshots -> view [style=dashed, arrowhead=normal, dir=forward];
}}
""")

# 5. job + incident state machines ---------------------------------------------------
render("states", f"""
digraph G {{
  rankdir=LR; {FONT}; node [{FONT}, fontsize=11, shape=box, style="rounded,filled", fillcolor="#ffffff"]; edge [{FONT}, fontsize=9, color="{GREY}"];
  subgraph cluster_job {{ label="job"; style=rounded; color="{GREEN}"; fontcolor="{GREEN}";
    q [label="queued"]; r [label="running\\n(lease)"]; d [label="done", color="{GREEN}"]; f [label="failed", color="{RED}"];
    q -> r [label="claim (SKIP LOCKED)"]; r -> d [label="complete"];
    r -> q [label="error, attempts < max"]; r -> r [label="lease expired:\\nreclaimed"];
    r -> f [label="attempts = max"];
  }}
  subgraph cluster_inc {{ label="incident"; style=rounded; color="{BLUE}"; fontcolor="{BLUE}";
    o [label="open"]; i [label="investigating"]; rs [label="resolved\\n(a candidate SUPPORTED,\\nawaiting human review)", color="{GREEN}"];
    na [label="no_action\\n(none supported /\\nno testable candidate)", color="{ORANGE}"];
    o -> i -> rs; i -> na;
  }}
}}
""")

# 6. module dependency map -------------------------------------------------------------
render("modules", f"""
digraph G {{
  rankdir=TB; nodesep=0.25; ranksep=0.4; {FONT}; node [{FONT}, fontsize=10, shape=box, style="rounded,filled", fillcolor="#ffffff"]; edge [color="#9aa0a8", arrowsize=0.6];
  cli [label="cli.py", color="{GREY}"];
  evaluation [label="evaluation.py\\ncalibration.py\\nreplication.py", color="{GREY}"];
  api [label="api/ (FastAPI + templates)", color="{GREEN}"];
  pipeline [label="pipeline.py\\n(investigate, experiment)", color="{GREEN}"];
  jobs [label="jobs/ queue + worker", color="{GREEN}"];
  telemetry [label="telemetry/ collector + detector", color="{BLUE}"];
  diagnosis [label="diagnosis/ rules + hypo", color="{PURPLE}"];
  plans [label="plans/ tree + features", color="{PURPLE}"];
  bench [label="bench/ harness, spec, params, env", color="{ORANGE}"];
  stats [label="stats/ verifier", color="{ORANGE}"];
  workload [label="workload/ packs (ecommerce, tpch),\\ngenerator, guardrail", color="{ORANGE}"];
  datagen [label="datagen/ generator + schema", color="{ORANGE}"];
  meta [label="meta/ schema + store", color="{GREEN}"];
  similarity [label="similarity/ features, search,\\nbenchmark", color="{GREY}"];
  db [label="db.py, config.py", color="{GREY}"];
  cli -> evaluation; cli -> jobs; cli -> api; cli -> bench;
  evaluation -> pipeline; evaluation -> telemetry; evaluation -> workload;
  api -> meta; api -> jobs; api -> similarity;
  jobs -> pipeline; pipeline -> telemetry; pipeline -> diagnosis; pipeline -> bench; pipeline -> workload; pipeline -> meta;
  diagnosis -> plans; workload -> stats; workload -> datagen; bench -> workload;
  telemetry -> meta; similarity -> meta; similarity -> plans;
  meta -> db; bench -> db; datagen -> db;
}}
""")

# 7. decision rule ---------------------------------------------------------------------
render("decision", f"""
digraph G {{
  rankdir=TB; nodesep=0.3; ranksep=0.35; {FONT}; node [{FONT}, fontsize=11]; edge [{FONT}, fontsize=10, color="{GREY}"];
  start [label="target effect: ratio B/A with 95% CI [L, U]\\n(paired block bootstrap)", shape=box, style=rounded];
  q0 [label="at least 10 blocks,\\nno timeouts?", shape=diamond];
  q1 [label="U < 0.95 ?", shape=diamond];
  q2 [label="L > 0.95 ?", shape=diamond];
  inc [label="INCONCLUSIVE", shape=box, style="rounded,filled", fillcolor="#fdf3e1", color="{ORANGE}"];
  rej [label="REJECTED\\n(no meaningful gain)", shape=box, style="rounded,filled", fillcolor="#fbe9ea", color="{RED}"];
  g [label="any guardrail query with\\nadjusted CI entirely above 1.10?\\n(confidence 1 - 0.05/m)", shape=diamond];
  sup [label="SUPPORTED\\n(awaiting human review)", shape=box, style="rounded,filled", fillcolor="#e6f4ec", color="{GREEN}"];
  rej2 [label="REJECTED\\n(collateral regression)", shape=box, style="rounded,filled", fillcolor="#fbe9ea", color="{RED}"];
  start -> q0; q0 -> inc [label="no"]; q0 -> q1 [label="yes"];
  q1 -> g [label="yes"]; q1 -> q2 [label="no"];
  q2 -> rej [label="yes"]; q2 -> inc [label="no (straddles)"];
  g -> rej2 [label="yes"]; g -> sup [label="no"];
}}
""")
print("diagrams:", sorted(p.name for p in OUT.glob("*.png")))
