import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cloudpulse.meta import store

pytestmark = pytest.mark.db
GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def client(test_meta_dsn, monkeypatch):
    monkeypatch.setenv("CP_META_DSN", test_meta_dsn)
    from cloudpulse.api.app import app
    return TestClient(app)


@pytest.fixture
def incident(test_meta_dsn):
    m = test_meta_dsn
    qpk = store.execute(m, "INSERT INTO queries (queryid, dbid, userid, query_text, template_name) "
                           "VALUES (42, 1, 1, 'SELECT ... WHERE customer_id = $1', 'orders_by_customer') "
                           "ON CONFLICT (queryid, dbid, userid, toplevel) DO UPDATE SET queryid = 42 "
                           "RETURNING id")[0]
    store.execute(m, "DELETE FROM performance_incidents WHERE query_id = %s", (qpk,))
    iid = store.execute(m, "INSERT INTO performance_incidents (query_id, detector, baseline_median_ms, "
                           "baseline_mad_ms, current_ms, ratio, evidence, status) VALUES "
                           "(%s, 'median_mad_v1', 1.0, 0.1, 40.0, 40.0, %s, 'resolved') RETURNING id",
                        (qpk, store.jsonb({"z": 50, "series": [1, 1.1, 0.9, 40, 41, 39],
                                           "anomalous_points": [3, 4, 5],
                                           "plan_changed_since_baseline": True})))[0]
    plan = json.loads((GOLDEN / "seq_scan_selective.json").read_text())
    store.execute(m, "INSERT INTO execution_plans (query_id, incident_id, source, fingerprint, plan) "
                     "VALUES (%s, %s, 'incident_explain_analyze', 'x', %s)", (qpk, iid, store.jsonb(plan)))
    store.execute(m, "INSERT INTO diagnoses (incident_id, rule, hypothesis, evidence, limitations) "
                     "VALUES (%s, 'selective_seq_scan', 'index may help', '{}', 'one sample')", (iid,))
    cid = store.execute(m, "INSERT INTO candidates (incident_id, kind, description, status) VALUES "
                           "(%s, 'index', 'CREATE INDEX ON orders (customer_id)', 'tested') RETURNING id",
                        (iid,))[0]
    eid = store.execute(m, "INSERT INTO experiments (candidate_id, protocol, status) VALUES "
                           "(%s, '{}', 'done') RETURNING id", (cid,))[0]
    effect = {"ratio": 0.02, "ci_low": 0.018, "ci_high": 0.023, "confidence": 0.95, "n_blocks": 15,
              "median_us_a": 30000, "median_us_b": 500, "block_ratios": [0.02, 0.021, 0.019]}
    store.execute(m, "INSERT INTO verdicts (experiment_id, label, reason, target, guardrail) VALUES "
                     "(%s, 'SUPPORTED', 'CI below 0.95', %s, %s)",
                  (eid, store.jsonb({"effect": effect}),
                   store.jsonb({"queries": [{**effect, "query": "touch_order", "ratio": 1.01,
                                             "weight": 20, "collateral_regression": False}],
                                "collateral": [], "weighted_change_pct": -60.0})))
    return iid


def test_health(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_pages_render(client, incident):
    r = client.get("/")
    assert r.status_code == 200 and "orders_by_customer" in r.text and "SUPPORTED" in r.text
    r = client.get(f"/incidents/{incident}")
    assert r.status_code == 200
    for text in ("Seq Scan", "CREATE INDEX ON orders (customer_id)", "touch_order", "-98.0%"):
        assert text in r.text


def test_json_api(client, incident):
    d = client.get(f"/api/incidents/{incident}").json()
    assert d["candidates"][0]["label"] == "SUPPORTED"
    assert client.get("/api/incidents/999999").status_code == 404


def test_investigate_enqueues_job(client, incident, test_meta_dsn):
    r = client.post(f"/api/incidents/{incident}/investigate",
                    json={"lab_state": {"scale": "tiny", "seed": 1, "sql": [], "db_settings": {}}})
    assert r.status_code == 202
    job = store.fetch_one(test_meta_dsn, "SELECT kind, status FROM jobs WHERE id = %s", (r.json()["job_id"],))
    assert job == {"kind": "investigate", "status": "queued"}
