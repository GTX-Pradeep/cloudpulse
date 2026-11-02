"""Index incidents as structural vectors in pgvector and retrieve similar ones.

Exact search (no ANN index): the table holds tens to thousands of rows, where a
sequential scan is fast and returns exact neighbours. An approximate index
(HNSW/IVFFlat) would only add recall loss and tuning without a measured need (D-018).
"""

from __future__ import annotations

from cloudpulse.meta import store
from cloudpulse.similarity import features as sf
from cloudpulse.workload.suite import BY_NAME


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def index_incident(meta_dsn: str, incident_id: int) -> dict | None:
    row = store.fetch_one(
        meta_dsn,
        "SELECT q.template_name, q.query_text, p.plan, p.findings FROM performance_incidents i "
        "JOIN queries q ON q.id = i.query_id "
        "LEFT JOIN execution_plans p ON p.incident_id = i.id AND p.source = 'incident_explain_analyze' "
        "WHERE i.id = %s", (incident_id,))
    if row is None:
        return None
    sql = BY_NAME[row["template_name"]].sql if row["template_name"] in BY_NAME else row["query_text"]
    feats = sf.sql_features(sql)
    if row["plan"]:
        from cloudpulse.plans import tree
        nodes = [n.node_type for n in tree.parse(row["plan"]).nodes()]
        feats.update(sf.plan_features(nodes, [f["kind"] for f in row["findings"] or []]))
    vec = sf.vectorize(feats)
    store.execute(
        meta_dsn,
        "INSERT INTO query_embeddings (incident_id, repr_version, features, embedding) "
        "VALUES (%s, %s, %s, %s::vector) ON CONFLICT (incident_id) DO UPDATE SET "
        "repr_version = EXCLUDED.repr_version, features = EXCLUDED.features, embedding = EXCLUDED.embedding",
        (incident_id, sf.REPR_VERSION, store.jsonb(feats), _vec_literal(vec)))
    return feats


def index_all(meta_dsn: str) -> int:
    ids = [r["id"] for r in store.fetch_all(meta_dsn, "SELECT id FROM performance_incidents")]
    for i in ids:
        index_incident(meta_dsn, i)
    return len(ids)


def similar(meta_dsn: str, incident_id: int, k: int = 5) -> list[dict]:
    return store.fetch_all(
        meta_dsn,
        "SELECT o.incident_id, round((o.embedding <=> e.embedding)::numeric, 4) AS distance, "
        "q.template_name, i.scenario, i.outcome, "
        "(SELECT string_agg(c.description || ' -> ' || coalesce(v.label, c.status), '; ' ORDER BY c.id) "
        "   FROM candidates c LEFT JOIN experiments x ON x.candidate_id = c.id "
        "   LEFT JOIN verdicts v ON v.experiment_id = x.id WHERE c.incident_id = o.incident_id) AS tested "
        "FROM query_embeddings e JOIN query_embeddings o ON o.incident_id <> e.incident_id "
        "JOIN performance_incidents i ON i.id = o.incident_id JOIN queries q ON q.id = i.query_id "
        "WHERE e.incident_id = %s ORDER BY o.embedding <=> e.embedding LIMIT %s",
        (incident_id, k))
