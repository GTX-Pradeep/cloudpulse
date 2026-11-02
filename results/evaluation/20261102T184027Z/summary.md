# CloudPulse ground-truth evaluation

- Environment label: **dev-cloud-sandbox (not official)**
- Captured: 2026-11-02T18:55:42+00:00 | git 8b77548-dirty
- Host: Intel(R) Xeon(R) Processor @ 2.80GHz (2 CPUs, 7.8 GB) | PostgreSQL 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
- Dataset: scale `default`, seed 42

| Scenario | Detected (rounds after fault) | False positives before fault | Diagnosis | Candidate → verdict |
|---|---|---|---|---|
| s0_control | — | none | — | — |
| s1_dropped_fk_index | yes (2); also flagged: latest_orders_for_customer | none | selective_seq_scan ✅ | `CREATE INDEX ON orders (customer_id)` → **SUPPORTED** (target -99.5%) |
| s2_stale_statistics | **no** | none | — ❌ | — |
| s3_work_mem_lowered | **no** | none | — ❌ | — |
| s4_updated_at_index_dropped | yes (2) | none | selective_seq_scan ✅ | `CREATE INDEX ON orders (updated_at)` → **SUPPORTED** (target -99.6%) |
| s5_created_at_index_dropped | yes (2) | recently_updated_orders | selective_seq_scan ✅ | `CREATE INDEX ON orders (created_at)` → **SUPPORTED** (target -99.7%) |
| s6_noisy_neighbor | —; also flagged: monthly_revenue_top_customers | recent_orders_feed, recently_updated_orders | — | `SET work_mem = '256MB' for this query's session` → **REJECTED** (target +6.2%) |

Raw evidence (plans, every timed execution, verdict reasons) is in the meta database and in `results/runs/`.
