# CloudPulse ground-truth evaluation

- Environment label: **dev-cloud-sandbox (not official)**
- Captured: 2026-11-02T19:14:55+00:00 | git 1fae662-dirty
- Host: Intel(R) Xeon(R) Processor @ 2.80GHz (2 CPUs, 7.8 GB) | PostgreSQL 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
- Dataset: scale `default`, seed 42

| Scenario | Measured effect of the fault on its query | Detected (rounds after fault) | False positives before fault | Diagnosis | Candidate → verdict |
|---|---|---|---|---|---|
| s0_control | — | — | none | — | — |
| s1_dropped_fk_index | 817.10× (`orders_by_customer`) | yes (1); also flagged: latest_orders_for_customer | none | selective_seq_scan ✅ | `CREATE INDEX ON orders (customer_id)` → **SUPPORTED** (target -99.5%) |
| s2_stale_statistics | 1.83× (`segment_revenue_for_status`) | **no** | none | — ❌ | — |
| s3_work_mem_lowered | 1.03× (`monthly_revenue_top_customers`) | **no** | none | — ❌ | — |
| s4_updated_at_index_dropped | 2635.15× (`recently_updated_orders`) | yes (2) | none | selective_seq_scan ✅ | `CREATE INDEX ON orders (updated_at)` → **REJECTED** (target -99.6%); collateral: touch_order |
| s5_created_at_index_dropped | 4752.46× (`recent_orders_feed`) | yes (2) | none | selective_seq_scan ✅ | `CREATE INDEX ON orders (created_at)` → **SUPPORTED** (target -99.6%) |
| s6_noisy_neighbor | — | —; also flagged: monthly_revenue_top_customers, segment_revenue_for_status | none | — | `SET work_mem = '256MB' for this query's session` → **REJECTED** (target +7.7%)<br>`CREATE INDEX ON orders (status)` → **SUPPORTED** (target -66.8%) |

Raw evidence (plans, every timed execution, verdict reasons) is in the meta database and in `results/runs/`.
