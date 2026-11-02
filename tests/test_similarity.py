import numpy as np

from cloudpulse.similarity import benchmark
from cloudpulse.similarity import features as sf


def test_sql_features_are_structural():
    f = sf.sql_features(
        "SELECT c.segment, count(*) FROM orders o JOIN customers c ON c.id = o.customer_id "
        "WHERE o.status = %s GROUP BY c.segment")
    assert f["tables"] == 2 and f["joins"] == 1 and f["aggregates"] == 1
    assert f["group_by"] == 1 and f["eq_predicates"] == 2 and f["is_select"] == 1


def test_arithmetic_is_not_a_predicate():
    f = sf.sql_features("UPDATE orders SET updated_at = updated_at + interval '1 second' WHERE id = %s")
    assert f["is_update"] == 1 and f["predicates"] == 1 and f["set_columns"] == 1


def test_surface_changes_do_not_change_the_vector():
    a = sf.vectorize(sf.sql_features("SELECT * FROM orders WHERE customer_id = 5"))
    b = sf.vectorize(sf.sql_features("select id from payments p where p.account_id = $1"))
    assert np.allclose(a, b)


def test_vectors_are_unit_length_and_fixed_size():
    v = sf.vectorize({**sf.sql_features("SELECT 1"), **sf.plan_features(["Seq Scan"], ["spill"])})
    assert len(v) == sf.DIM and abs(np.linalg.norm(v) - 1) < 1e-9


def test_benchmark_beats_text_baseline():
    r = benchmark.run(k=4)
    assert r["recall_at_k_structural"] > r["recall_at_k_text_jaccard"]
