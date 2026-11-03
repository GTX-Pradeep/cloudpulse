"""Golden-file tests: real EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) output captured
from PostgreSQL 16 on the generated dataset (tests/golden/)."""

import json
from pathlib import Path

import pytest

from cloudpulse.plans import features, tree

GOLDEN = Path(__file__).parent / "golden"


def load(name):
    return tree.parse(json.loads((GOLDEN / name).read_text()))


def kinds(plan):
    return [f.kind for f in features.extract(plan, {"orders": 2.5e6, "customers": 1e5})]


def test_parse_accepts_list_dict_and_string():
    raw = json.loads((GOLDEN / "pk_lookup.json").read_text())
    a, b, c = tree.parse(raw), tree.parse(raw[0]), tree.parse(json.dumps(raw))
    assert tree.fingerprint(a) == tree.fingerprint(b) == tree.fingerprint(c)


def test_seq_scan_selectivity_and_columns():
    p = load("seq_scan_selective.json")
    f = next(x for x in features.extract(p) if x.kind == "filtered_seq_scan")
    assert f.relation == "orders"
    assert f.detail["equality_columns"] == ["customer_id"]
    assert f.detail["selectivity"] < 0.01
    assert f.detail["rows_examined"] > 2_000_000


def test_misestimate_found_on_scan_not_on_aggregates():
    p = load("misestimate_nested_loop.json")
    mis = [f for f in features.extract(p) if f.kind == "misestimate"]
    assert mis and all(f.relation for f in mis)
    assert max(f.detail["q_error"] for f in mis) > 1000
    assert "repeated_inner_work" in kinds(p)


def test_hashagg_spill_detected_once():
    p = load("hashagg_spill.json")
    spills = [f for f in features.extract(p) if f.kind == "spill"]
    assert len(spills) == 1  # only the aggregate, not its Sort/Limit ancestors
    assert spills[0].detail["batches"] > 1


def test_index_walk_with_heavy_filter():
    p = load("index_walk_heavy_filter.json")
    f = next(x for x in features.extract(p) if x.kind == "index_scan_heavy_filter")
    assert f.detail["index"] == "orders_created_at_idx"
    assert f.detail["rows_kept"] == 10


def test_healthy_pk_lookup_has_no_problem_findings():
    assert set(kinds(load("pk_lookup.json"))) <= {"hotspot"}


def test_estimate_only_plan_has_no_actuals():
    p = load("estimate_only.json")
    assert not p.analyzed and all(n.q_error is None for n in p.nodes())


def test_self_time_never_negative():
    for f in GOLDEN.glob("*.json"):
        for n in tree.parse(json.loads(f.read_text())).nodes():
            assert n.self_time_ms is None or n.self_time_ms >= 0


def test_fingerprint_ignores_costs_and_constants():
    raw = json.loads((GOLDEN / "seq_scan_selective.json").read_text())
    other = json.loads(json.dumps(raw))
    other[0]["Plan"]["Total Cost"] = 1.0
    other[0]["Plan"]["Filter"] = "(customer_id = '999'::bigint)"
    other[0]["Plan"]["Actual Rows"] = 5
    assert tree.fingerprint(tree.parse(raw)) == tree.fingerprint(tree.parse(other))


def test_fingerprint_changes_with_plan_shape():
    fps = {tree.fingerprint(tree.parse(json.loads(f.read_text()))) for f in GOLDEN.glob("*.json")}
    assert len(fps) == len(list(GOLDEN.glob("*.json")))


@pytest.mark.parametrize("expr,cols,eq", [
    ("(customer_id = '42'::bigint)", ["customer_id"], ["customer_id"]),
    ("((status = 'pending'::text) AND (o.created_at > '2025-01-01'::timestamp))",
     ["status", "created_at"], ["status"]),
    ("(updated_at > $1)", ["updated_at"], []),
    (None, [], []),
])
def test_filter_column_extraction(expr, cols, eq):
    assert features.filter_columns(expr) == cols
    assert features.equality_columns(expr) == eq


def test_hotspot_share_is_a_fraction_even_for_parallel_plans():
    """F-12: parallel workers count as loops, so self time can exceed wall-clock time."""
    for f in GOLDEN.glob("*.json"):
        for x in features.extract(tree.parse(json.loads(f.read_text()))):
            if x.kind == "hotspot":
                assert 0 < x.detail["share"] <= 1.0
