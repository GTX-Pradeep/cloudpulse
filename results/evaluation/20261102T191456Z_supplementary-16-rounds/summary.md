# CloudPulse ground-truth evaluation

- Environment label: **dev-cloud-sandbox (not official)**
- Captured: 2026-11-02T19:18:54+00:00 | git 49f548d-dirty
- Host: Intel(R) Xeon(R) Processor @ 2.80GHz (2 CPUs, 7.8 GB) | PostgreSQL 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
- Dataset: scale `default`, seed 42

| Scenario | Measured effect of the fault on its query | Detected (rounds after fault) | False positives before fault | Diagnosis | Candidate → verdict |
|---|---|---|---|---|---|
| s2_stale_statistics | 1.84× (`segment_revenue_for_status`) | yes (9) | none | stale_statistics ✅ | `ANALYZE orders` → **SUPPORTED** (target -54.9%) |

Raw evidence (plans, every timed execution, verdict reasons) is in the meta database and in `results/runs/`.
