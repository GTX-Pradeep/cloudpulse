# Replication of candidate verdicts

- Environment: **dev-cloud-sandbox (not official)** | PostgreSQL 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1) | git ba2df76-dirty
- Each candidate re-run 3 times with fresh twins and new shuffle seeds.

| Scenario | Candidate | Original verdict | Replications | Consolidated | Target time ratio per run | Collateral per run |
|---|---|---|---|---|---|---|
| s1_dropped_fk_index | `CREATE INDEX ON orders (customer_id)` | SUPPORTED (0.005) | REJECTED ×1, SUPPORTED ×2 | **UNSTABLE** | 0.005, 0.005, 0.004 | touch_order 1.44×; none; none |
| s1_dropped_fk_index | `CREATE INDEX ON orders (customer_id, created_at)` | SUPPORTED (0.003) | SUPPORTED ×3 | **SUPPORTED** | 0.002, 0.002, 0.001 | none; none; none |
| s2_stale_statistics | `ANALYZE orders` | SUPPORTED (0.451) | SUPPORTED ×3 | **SUPPORTED** | 0.434, 0.440, 0.447 | none; none; none |
| s4_updated_at_index_dropped | `CREATE INDEX ON orders (updated_at)` | REJECTED (0.004) | SUPPORTED ×3 | **UNSTABLE** | 0.003, 0.003, 0.003 | none; none; none |
| s5_created_at_index_dropped | `CREATE INDEX ON orders (created_at)` | REJECTED (0.003) | REJECTED ×3 | **REJECTED** | 0.003, 0.002, 0.003 | latest_orders_for_customer 4.44×; latest_orders_for_customer 5.62×; latest_orders_for_customer 4.64× |
| s6_noisy_neighbor | `CREATE INDEX ON orders (status)` | SUPPORTED (0.332) | SUPPORTED ×3 | **SUPPORTED** | 0.352, 0.350, 0.372 | none; none; none |
| s6_noisy_neighbor | `SET work_mem = '256MB' for this query's session` | REJECTED (1.077) | REJECTED ×3 | **REJECTED** | 1.051, 1.050, 1.057 | none; none; none |
