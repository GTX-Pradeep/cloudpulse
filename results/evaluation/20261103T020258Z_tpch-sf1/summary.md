# CloudPulse ground-truth evaluation

- Environment label: **dev-cloud-sandbox (not official)**
- Captured: 2026-11-03T02:10:00+00:00 | git c875c05-dirty
- Host: Intel(R) Xeon(R) Processor @ 2.30GHz (2 CPUs, 7.8 GB) | PostgreSQL 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
- Dataset: scale `tpch-sf1`, seed 42

| Scenario | Measured effect of the fault on its query | Detected (rounds after fault) | False positives before fault | Diagnosis | Candidate → verdict |
|---|---|---|---|---|---|
| t0_control | — | — | none | — | — |
| t1_shipdate_index_dropped | 2.50× (`tpch_q14_promotion_effect`) | yes (4) | none | selective_seq_scan ✅ | `CREATE INDEX ON lineitem (l_shipdate)` → **SUPPORTED** (target -43.8%) |
| t2_custkey_index_dropped | 761.45× (`tpch_customer_recent_orders`) | yes (3) | tpch_q3_shipping_priority | selective_seq_scan ✅ | `CREATE INDEX ON orders (o_custkey)` → **SUPPORTED** (target -99.7%) |
| t3_orderdate_index_dropped | 0.77× (`tpch_q4_order_priority`) | nothing to detect (fault effect below 1.5×) | tpch_q14_promotion_effect, tpch_q3_shipping_priority | — | — |

Raw evidence (plans, every timed execution, verdict reasons) is in the meta database and in `results/runs/`.
