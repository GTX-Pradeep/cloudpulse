# CloudPulse ground-truth evaluation

- Environment label: **dev-cloud-sandbox (not official)**
- Captured: 2026-11-02T19:21:23+00:00 | git 743e818-dirty
- Host: Intel(R) Xeon(R) Processor @ 2.80GHz (2 CPUs, 7.8 GB) | PostgreSQL 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
- Dataset: scale `default`, seed 42

| Scenario | Measured effect of the fault on its query | Detected (rounds after fault) | False positives before fault | Diagnosis | Candidate → verdict |
|---|---|---|---|---|---|
| s5_created_at_index_dropped | 6143.67× (`recent_orders_feed`) | yes (3) | none | selective_seq_scan ✅ | `CREATE INDEX ON orders (created_at)` → **REJECTED** (target -99.7%); collateral: latest_orders_for_customer; ⚠ `touch_order` +4.7% (CI +0.1%…+8.5%, below veto margin) |

Raw evidence (plans, every timed execution, verdict reasons) is in the meta database and in `results/runs/`.
