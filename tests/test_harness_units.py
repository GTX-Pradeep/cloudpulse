import pytest

from cloudpulse.bench import harness
from cloudpulse.bench.spec import ExperimentSpec, QuerySpec
from cloudpulse.config import Settings
from cloudpulse.db import SafetyError, assert_not_target, check_ident, with_database
from cloudpulse.stats import verifier
from cloudpulse.workload import guardrail
from tests.test_stats import synth_rows


def test_arm_order_alternates():
    assert [harness.block_order(b) for b in range(4)] == [("A", "B"), ("B", "A"), ("A", "B"), ("B", "A")]


def test_block_permutation_is_deterministic_and_complete():
    a = harness.block_permutation(20, 1, 0, 3)
    assert a == harness.block_permutation(20, 1, 0, 3)
    assert sorted(a) == list(range(20))
    assert a != harness.block_permutation(20, 1, 0, 4)


def test_base_key_changes_with_state():
    k = harness.base_key("small", 42, [])
    assert k == harness.base_key("small", 42, [])
    assert k != harness.base_key("small", 43, [])
    assert k != harness.base_key("small", 42, ["DROP INDEX x"])


def test_spec_round_trip():
    spec = ExperimentSpec.from_dict({
        "name": "x", "queries": [{"name": "q", "sql": "SELECT 1"}],
        "arm_b": {"setup_sql": ["CREATE INDEX i ON t (c)"]}, "protocol": {"blocks": 3},
    })
    assert spec.arm_a.name == "A" and spec.arm_b.setup_sql
    assert spec.protocol.blocks == 3
    with pytest.raises(ValueError):
        QuerySpec("bad", "SELECT 1", kind="delete")


def test_refuses_to_experiment_on_target():
    s = Settings("postgresql://u@localhost:5433/target", "postgresql://u@127.0.0.1:5433/other",
                 "postgresql://u@localhost:5435/m", "t")
    with pytest.raises(SafetyError):
        assert_not_target(s, s.lab_dsn)
    s2 = Settings(s.target_dsn, "postgresql://u@localhost:5434/lab", s.meta_dsn, "t")
    assert_not_target(s2, s2.lab_dsn)


def test_identifier_guard():
    assert with_database("postgresql://u@h:1/a", "cp_exp_a").endswith("/cp_exp_a")
    for bad in ("x; drop", "Upper", "a-b", ""):
        with pytest.raises(ValueError):
            check_ident(bad)


def test_guardrail_veto_and_pass():
    target = QuerySpec("t", "SELECT 1")
    other = QuerySpec("o", "SELECT 2", weight=5)
    rows = synth_rows(20, 10, 0.3, noise=0.05, seed=1, query="t") + \
        synth_rows(20, 10, 1.6, noise=0.05, seed=2, query="o")
    rep = guardrail.evaluate(rows, target, [other])
    assert rep.label == verifier.REJECTED and rep.collateral == ["o"]
    assert "regressed" in rep.reason

    rows_ok = synth_rows(20, 10, 0.3, noise=0.05, seed=1, query="t") + \
        synth_rows(20, 10, 1.0, noise=0.05, seed=3, query="o")
    rep_ok = guardrail.evaluate(rows_ok, target, [other])
    assert rep_ok.label == verifier.SUPPORTED and rep_ok.collateral == []
    assert rep_ok.weighted_change_pct < 0


def test_guardrail_warns_on_confident_small_slowdown():
    target = QuerySpec("t", "SELECT 1")
    other = QuerySpec("o", "SELECT 2")
    rows = synth_rows(20, 10, 0.3, noise=0.02, seed=1, query="t") + \
        synth_rows(20, 10, 1.05, noise=0.02, block_noise=0.01, seed=4, query="o")
    rep = guardrail.evaluate(rows, target, [other])
    assert rep.label == verifier.SUPPORTED  # below the 10% veto margin: rule unchanged
    assert rep.warnings == ["o"] and rep.collateral == []


def test_union_sampler_adds_required_values_once():
    from cloudpulse.bench import params as pm
    spec = {"type": "union", "base": {"type": "values", "values": [[1], [2]]}, "extra": [[2], [23]]}
    assert pm.sample(spec, "unused", 0) == [(1,), (2,), (23,)]


def test_guardrail_queries_include_plan_changing_params():
    from cloudpulse import pipeline
    from cloudpulse.workload.suite import BY_NAME
    cand = {"kind": "index", "hypopg": {"affected_reads": ["latest_orders_for_customer"],
                                        "writes_on_table": ["touch_order"],
                                        "changed_params": {"latest_orders_for_customer": [[23]]}}}
    g = {q.name: q for q in pipeline.guardrail_queries(cand, BY_NAME["recent_orders_feed"])}
    assert g["latest_orders_for_customer"].params["type"] == "union"
    assert g["latest_orders_for_customer"].params["extra"] == [[23]]
    assert g["touch_order"].params["type"] == "order_ids"


def test_union_sampler_combines_samplers():
    from cloudpulse.bench import params as pm
    spec = {"type": "union", "base": {"type": "values", "values": [[1]]},
            "also": [{"type": "values", "values": [[1], [5]]}]}
    assert pm.sample(spec, "unused", 0) == [(1,), (5,)]
