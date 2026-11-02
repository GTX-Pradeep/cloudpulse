import json
from pathlib import Path

from cloudpulse.diagnosis import rules
from cloudpulse.plans import features, tree

GOLDEN = Path(__file__).parent / "golden"


def ctx(existing=None, mod=0, work_mem="4MB"):
    return rules.Context(
        existing_indexes=existing if existing is not None else {"orders": [["id"]], "customers": [["id"]]},
        table_stats={"orders": {"reltuples": 2.3e6, "n_mod_since_analyze": mod},
                     "customers": {"reltuples": 1e5, "n_mod_since_analyze": 0}},
        settings={"work_mem": work_mem},
        index_columns={"orders_created_at_idx": ["created_at"]},
    )


def diagnose(name, c):
    p = tree.parse(json.loads((GOLDEN / name).read_text()))
    return rules.diagnose(p, features.extract(p, {"orders": 2.3e6, "customers": 1e5}), c)


def candidates(diags):
    return [c.description for d in diags for c in d.candidates]


def test_selective_seq_scan_proposes_index():
    d = diagnose("seq_scan_selective.json", ctx())
    assert d[0].rule == "selective_seq_scan"
    assert candidates(d) == ["CREATE INDEX ON orders (customer_id)"]
    assert d[0].limitations


def test_existing_index_is_not_proposed_again():
    d = diagnose("seq_scan_selective.json", ctx({"orders": [["customer_id"]]}))
    assert candidates(d) == []
    assert "already exists" in d[0].limitations


def test_stale_statistics_proposes_analyze_only_when_stale():
    stale = diagnose("misestimate_nested_loop.json", ctx(mod=200_000))
    assert "stale_statistics" in [x.rule for x in stale]
    assert "ANALYZE orders" in candidates(stale)
    fresh = diagnose("misestimate_nested_loop.json", ctx(mod=0))
    assert "estimation_limit" in [x.rule for x in fresh]
    assert "ANALYZE orders" not in candidates(fresh)


def test_spill_proposes_scoped_work_mem():
    d = diagnose("hashagg_spill.json", ctx())
    spill = next(x for x in d if x.rule == "work_mem_spill")
    c = spill.candidates[0]
    assert c.kind == "setting" and c.setup_sql == []
    assert c.session["work_mem"].endswith("MB")
    assert int(c.session["work_mem"][:-2]) <= rules.WORK_MEM_CAP_MB


def test_index_walk_proposes_composite_index():
    d = diagnose("index_walk_heavy_filter.json", ctx())
    assert "CREATE INDEX ON orders (customer_id, created_at)" in candidates(d)


def test_healthy_plan_gives_cause_unclear():
    d = diagnose("pk_lookup.json", ctx())
    assert [x.rule for x in d] == ["cause_unclear"]
    assert candidates(d) == []


def test_index_candidate_sql_is_well_formed():
    c = rules.index_candidate("orders", ["customer_id", "created_at"])
    assert c.setup_sql == [
        "CREATE INDEX cp_orders_customer_id_created_at_idx ON orders (customer_id, created_at)"
    ]
