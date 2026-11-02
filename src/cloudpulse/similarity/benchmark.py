"""Retrieval benchmark for the structural vectors.

Six structural families, five variants each. Variants change surface details
(constants, aliases, selected columns, table names, case and whitespace) but
keep the structure. For every query we ask: are the other members of its family
in the top-k neighbours? We compare against a plain-text baseline (token Jaccard).

Honest limitation: the families are hand-written and the features were designed
with this kind of structure in mind, so good numbers here show the features
behave as intended, not that they generalise to arbitrary real workloads.
"""

from __future__ import annotations

import re

import numpy as np

from cloudpulse.similarity import features as sf

FAMILIES: dict[str, list[str]] = {
    "point_lookup": [
        "SELECT * FROM orders WHERE id = 5",
        "select id, status from orders o where o.id = 99",
        "SELECT name FROM customers WHERE id = $1",
        "SELECT amount   FROM payments WHERE payment_id = 7",
        "select sku, price from products p where p.product_id = 3",
    ],
    "filtered_topn": [
        "SELECT id FROM orders WHERE customer_id = 1 ORDER BY created_at DESC LIMIT 10",
        "select * from orders o where o.customer_id = $1 order by o.created_at desc limit 5",
        "SELECT id, total FROM invoices WHERE account_id = 9 ORDER BY issued_at LIMIT 20",
        "SELECT body FROM messages WHERE thread_id = 4 ORDER BY sent_at DESC LIMIT 50",
        "select x from events e where e.user_id = 3 order by e.ts desc limit 1",
    ],
    "join_aggregate": [
        "SELECT c.segment, count(*) FROM orders o JOIN customers c ON c.id = o.customer_id "
        "GROUP BY c.segment",
        "select p.category, sum(i.qty) from items i join products p on p.id = i.product_id "
        "group by p.category",
        "SELECT r.name, avg(s.amount) FROM sales s JOIN regions r ON r.id = s.region_id GROUP BY r.name",
        "SELECT t.team, max(m.score) FROM matches m JOIN teams t ON t.id = m.team_id GROUP BY t.team",
        "select d.name, count(*) from employees e join departments d on d.id = e.dept_id group by d.name",
    ],
    "range_scan": [
        "SELECT id FROM orders WHERE created_at > '2025-01-01'",
        "select * from logs where ts >= $1",
        "SELECT id, amount FROM payments WHERE paid_at < now()",
        "select v from readings r where r.measured_at > $1",
        "SELECT * FROM sessions WHERE started_at <= '2024-06-01'",
    ],
    "single_row_update": [
        "UPDATE orders SET updated_at = now() WHERE id = 1",
        "update customers set name = $1 where id = $2",
        "UPDATE accounts SET balance = 0 WHERE account_id = 5",
        "update tasks t set done = true where t.id = 9",
        "UPDATE products SET price = 10 WHERE sku = 'x'",
    ],
    "report_group_topn": [
        "SELECT customer_id, sum(amount) AS s FROM orders GROUP BY customer_id ORDER BY s DESC LIMIT 20",
        "select author, count(*) c from posts group by author order by c desc limit 10",
        "SELECT page, count(*) FROM hits GROUP BY page ORDER BY 2 DESC LIMIT 5",
        "select region, avg(t) from temps group by region order by 2 limit 3",
        "SELECT sku, sum(qty) q FROM sales GROUP BY sku ORDER BY q DESC LIMIT 100",
    ],
}


def _tokens(sql: str) -> set[str]:
    return set(re.findall(r"[a-z_]+", sql.lower()))


def _recall_at_k(sim: np.ndarray, labels: list[str], k: int) -> float:
    hits = total = 0
    for i, lab in enumerate(labels):
        order = [j for j in np.argsort(-sim[i], kind="stable") if j != i][:k]
        same = sum(1 for j in range(len(labels)) if j != i and labels[j] == lab)
        hits += sum(labels[j] == lab for j in order)
        total += min(k, same)
    return hits / total


def run(k: int = 4) -> dict:
    labels, sqls = [], []
    for fam, qs in FAMILIES.items():
        labels += [fam] * len(qs)
        sqls += qs
    vecs = np.array([sf.vectorize(sf.sql_features(s)) for s in sqls])
    structural = vecs @ vecs.T
    toks = [_tokens(s) for s in sqls]
    jac = np.array([[len(a & b) / len(a | b) for b in toks] for a in toks])
    return {
        "k": k, "n_queries": len(sqls), "families": len(FAMILIES),
        "recall_at_k_structural": round(_recall_at_k(structural, labels, k), 3),
        "recall_at_k_text_jaccard": round(_recall_at_k(jac, labels, k), 3),
    }
