"""Result charts for the guide, computed from committed result files and the meta DB."""
import csv
import glob
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent / "img"
OUT.mkdir(exist_ok=True)
BLUE, ORANGE, GREEN, RED, GREY = "#2f5bd3", "#c96f12", "#1f7a4d", "#b4232c", "#7a808a"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False,
                     "axes.spines.right": False, "axes.titleweight": "bold", "axes.titlesize": 11,
                     "figure.dpi": 200})


def raw(run_dir):
    return list(csv.DictReader(open(run_dir / "raw.csv")))


def chart_s5_customers():
    run = sorted(REPO.glob("results/runs/*cand8_recent_orders_feed"))[-1]
    meta = json.load(open(run / "meta.json"))
    params = meta["params"]["latest_orders_for_customer"]
    by = defaultdict(list)
    for r in raw(run):
        if r["query"] == "latest_orders_for_customer":
            by[(int(r["param_index"]), r["arm"])].append(float(r["latency_us"]) / 1000)
    rows = [(params[i][0], st.median(by[(i, "A")]), st.median(by[(i, "B")])) for i in range(len(params))]
    rows.sort(key=lambda r: -r[2] / r[1])
    top = rows[:10]
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    x = np.arange(len(top))
    ax.bar(x - 0.2, [r[1] for r in top], 0.4, color=BLUE, label="without created_at index (arm A)")
    ax.bar(x + 0.2, [r[2] for r in top], 0.4, color=RED, label="with created_at index (arm B)")
    for i, r in enumerate(top):
        ax.text(i + 0.2, r[2] * 1.15, f"{r[2] / r[1]:.1f}×", ha="center", fontsize=8, color=RED)
    ax.set_yscale("log")
    ax.set_xticks(x, [f"cust {r[0]}" for r in top], rotation=30, ha="right", fontsize=8)
    ax.set_ylabel("median latency (ms, log scale)")
    ax.set_title("S5: 'latest 10 orders of customer X', most affected customers")
    ax.set_ylim(top=max(r[2] for r in top) * 4)
    ax.legend(frameon=False, fontsize=8, loc="upper right", ncol=2)
    fig.tight_layout()
    fig.savefig(OUT / "chart_s5_customers.png")
    plt.close(fig)
    return {"run": str(run.relative_to(REPO)), "rows": top, "n_params": len(rows),
            "n_faster": sum(1 for r in rows if r[2] < 0.9 * r[1]),
            "n_slower15": sum(1 for r in rows if r[2] > 1.5 * r[1])}


def chart_detection():
    import psycopg
    rows = psycopg.connect("postgresql://cloudpulse@localhost:5435/cloudpulse").execute(
        "SELECT i.scenario, q.template_name, i.evidence->'series', i.evidence->'anomalous_points', "
        "i.baseline_median_ms FROM performance_incidents i JOIN queries q ON q.id = i.query_id "
        "WHERE i.scenario IN ('s1_dropped_fk_index','s4_updated_at_index_dropped','s5_created_at_index_dropped',"
        "'s2_stale_statistics') ORDER BY i.id").fetchall()
    seen = set()
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    colors = [BLUE, GREEN, ORANGE, RED]
    k = 0
    for sc, q, series, anom, med in rows:
        if sc in seen:
            continue
        seen.add(sc)
        y = [v / med if v else None for v in series]
        tag = " (16-interval supplementary run)" if sc.startswith("s2") else ""
        ax.plot(range(len(y)), y, marker="o", ms=2.5, lw=1.4, color=colors[k],
                label=f"{sc.split('_', 1)[0].upper()}: {q}{tag}")
        ax.scatter(anom, [y[i] for i in anom], s=28, facecolors="none", edgecolors=colors[k], lw=1.2)
        k += 1
    ax.axvline(13.5, color=GREY, ls="--", lw=1)
    ax.text(13.6, 1.3, "fault injected", fontsize=8, color=GREY)
    ax.set_yscale("log")
    ax.set_xlabel("telemetry interval")
    ax.set_ylabel("interval mean / baseline median (log)")
    ax.set_title("Detection: each query compared with its own history (circles = anomalous)")
    ax.legend(frameon=False, fontsize=7, loc="upper left")
    fig.tight_layout()
    fig.savefig(OUT / "chart_detection.png")
    plt.close(fig)


def chart_calibration():
    s = json.load(open(sorted(REPO.glob("results/calibration/*/summary.json"))[-1]))
    by = defaultdict(list)
    for r in s["records"]:
        by[r["query"]].append(r)
    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    names = list(by)
    for i, q in enumerate(names):
        for r in by[q]:
            ok = r["ci_contains_1"]
            ax.errorbar(r["ratio"], i + (r["repeat"] - 4.5) * 0.05,
                        xerr=[[r["ratio"] - r["ci_low"]], [r["ci_high"] - r["ratio"]]],
                        fmt="o", ms=3, lw=0.8, color=BLUE if ok else RED, alpha=0.85)
    ax.axvspan(0.95, 1.05, color=GREEN, alpha=0.08)
    ax.axvline(1.0, color=GREY, lw=1)
    ax.axvline(0.95, color=GREEN, lw=0.8, ls="--")
    ax.axvline(1.10, color=RED, lw=0.8, ls="--")
    ax.text(0.949, -0.75, "SUPPORTED needs CI < 0.95 ", fontsize=7, color=GREEN, ha="right")
    ax.text(1.103, -0.75, "veto needs CI > 1.10", fontsize=7, color=RED)
    ax.set_ylim(-1.0, len(names) - 0.4)
    ax.set_yticks(range(len(names)), names, fontsize=8)
    ax.set_xlabel("time ratio B/A with 95% CI (identical arms: truth is 1.0)")
    ax.set_title(f"A/A calibration: 10 repeats × 5 queries (red = CI misses 1.0)")
    fig.tight_layout()
    fig.savefig(OUT / "chart_calibration.png")
    plt.close(fig)


def chart_bootstrap():
    run = sorted(REPO.glob("results/runs/*cand8_recent_orders_feed"))[-1]
    rows = raw(run)
    sums = defaultdict(float)
    for r in rows:
        if r["query"] == "latest_orders_for_customer" and r["status"] == "ok":
            sums[(int(r["block"]), r["arm"])] += float(r["latency_us"])
    blocks = sorted({b for b, _ in sums})
    d = np.array([math.log(sums[(b, "B")] / sums[(b, "A")]) for b in blocks])
    rng = np.random.default_rng(0)
    means = d[rng.integers(0, len(d), size=(10000, len(d)))].mean(axis=1)
    lo, hi = np.quantile(means, [0.0125, 0.9875])
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.9), gridspec_kw={"width_ratios": [1, 1.4]})
    axes[0].scatter(range(len(d)), np.exp(d), color=RED, s=18)
    axes[0].axhline(1.0, color=GREY, lw=1)
    axes[0].axhline(1.10, color=RED, lw=0.8, ls="--")
    axes[0].set_xlabel("block")
    axes[0].set_ylabel("block time ratio B/A")
    axes[0].set_title("15 paired blocks")
    axes[1].hist(np.exp(means), bins=60, color=RED, alpha=0.7)
    for v in (lo, hi):
        axes[1].axvline(math.exp(v), color="black", lw=1)
    axes[1].axvline(1.10, color=RED, lw=0.8, ls="--")
    axes[1].set_xlabel("mean ratio in each bootstrap resample")
    axes[1].set_title(f"10,000 resamples -> 97.5% CI [{math.exp(lo):.2f}, {math.exp(hi):.2f}]")
    fig.suptitle("S5 guardrail query latest_orders_for_customer: how the interval is built", fontsize=10, fontweight="bold")
    fig.tight_layout()
    fig.savefig(OUT / "chart_bootstrap.png")
    plt.close(fig)


def chart_replication():
    files = sorted(REPO.glob("results/replication/*/summary.json"))
    if not files:
        return None
    s = json.load(open(files[-1]))
    cands = s["candidates"] if isinstance(s, dict) else s
    fig, ax = plt.subplots(figsize=(7.4, 0.55 * len(cands) + 1.5))
    color = {"SUPPORTED": GREEN, "REJECTED": RED, "INCONCLUSIVE": ORANGE}
    labels = []
    for i, c in enumerate(cands):
        short = c["candidate"].replace("CREATE INDEX ON orders ", "index ").replace(" for this query's session", "")
        labels.append(f"{c['scenario'].split('_', 1)[0].upper()}: {short}")
        pts = [(c["original_ratio"], c["original_label"], "D")] + \
              [(r["ratio"], r["label"], "o") for r in c["replications"]]
        for j, (x, lab, m) in enumerate(pts):
            ax.scatter(x, i + (j - 1.5) * 0.12, color=color[lab], marker=m, s=30, zorder=3)
    ax.set_xscale("log")
    ax.axvline(1.0, color=GREY, lw=1)
    ax.axvline(0.95, color=GREEN, lw=0.8, ls="--")
    ax.set_yticks(range(len(cands)), labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("target time ratio B/A (log scale)\ndiamond = original run, dots = replications, colour = verdict")
    ax.set_title("Replication: same candidate, fresh twins, new seeds")
    for lab, col in color.items():
        ax.scatter([], [], color=col, label=lab, s=25)
    ax.legend(frameon=True, fontsize=8, loc="lower left")
    fig.tight_layout()
    fig.savefig(OUT / "chart_replication.png")
    plt.close(fig)
    return True


def chart_tpch():
    files = sorted(REPO.glob("results/evaluation/*tpch*/summary.json"))
    if not files:
        return None
    s = json.load(open(files[-1]))
    names, ratios = [], []
    for sc in s["scenarios"]:
        q = sc["expected_query"]
        if q and q in sc["observed_ratio"]:
            names.append(f"{sc['scenario'].split('_', 1)[0].upper()}: {q}")
            ratios.append(sc["observed_ratio"][q])
    fig, ax = plt.subplots(figsize=(7.2, 2.4))
    ax.barh(names, ratios, color=BLUE)
    ax.axvline(1.5, color=RED, ls="--", lw=0.8)
    ax.text(1.52, -0.45, "detector needs ≥ 1.5×", fontsize=7, color=RED)
    ax.set_xscale("log")
    ax.set_xlabel("measured slowdown of the query after the fault (log)")
    ax.set_title("TPC-H: effect of each injected fault")
    fig.tight_layout()
    fig.savefig(OUT / "chart_tpch.png")
    plt.close(fig)
    return True


if __name__ == "__main__":
    which = sys.argv[1:] or ["s5", "detection", "calibration", "bootstrap", "replication", "tpch"]
    info = {}
    if "s5" in which:
        info["s5"] = chart_s5_customers()
    if "detection" in which:
        chart_detection()
    if "calibration" in which:
        chart_calibration()
    if "bootstrap" in which:
        chart_bootstrap()
    if "replication" in which:
        chart_replication()
    if "tpch" in which:
        chart_tpch()
    (OUT / "chart_info.json").write_text(json.dumps(info, indent=1, default=str))
    print(sorted(p.name for p in OUT.glob("chart_*.png")))
