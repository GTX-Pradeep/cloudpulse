"""The TPC-H workload pack: a workload CloudPulse was not designed around.

TPC-H is the industry-standard decision-support benchmark (tpc.org). Its data
is fully specified, so the generator (tpchgen-cli, an implementation of the
official dbgen) produces identical data for a given scale factor. No seed involved.

The tracked workload mixes OLTP-style lookups on the TPC-H schema with several
official TPC-H queries (Q3, Q4, Q6, Q14) whose substitution parameters are drawn
deterministically, as the specification describes, plus an UPDATE. The healthy
baseline adds the secondary indexes a typical deployment would have; each fault
removes one of them. CloudPulse's rules, thresholds and decision rule were all
written for the e-commerce pack and are used here unchanged.
"""

from __future__ import annotations

import shutil
import subprocess
from importlib import resources
from pathlib import Path

from cloudpulse import db
from cloudpulse.bench.spec import QuerySpec
from cloudpulse.config import REPO_ROOT
from cloudpulse.workload.pack import Pack, Scenario

SCALE_FACTORS = {"tpch-sf0.01": "0.01", "tpch-sf0.1": "0.1", "tpch-sf1": "1"}
TABLES = ["region", "nation", "supplier", "part", "partsupp", "customer", "orders", "lineitem"]
PRIMARY_KEYS = {
    "region": "r_regionkey", "nation": "n_nationkey", "supplier": "s_suppkey",
    "part": "p_partkey", "partsupp": "ps_partkey, ps_suppkey", "customer": "c_custkey",
    "orders": "o_orderkey", "lineitem": "l_orderkey, l_linenumber",
}
CACHE = REPO_ROOT / ".cache" / "tpch"


def schema_sql() -> str:
    return resources.files("cloudpulse.workload").joinpath("tpch_schema.sql").read_text()


def generator_version() -> str:
    exe = shutil.which("tpchgen-cli")
    if not exe:
        raise RuntimeError("tpchgen-cli not found: pip install tpchgen-cli")
    return subprocess.run([exe, "-V"], capture_output=True, text=True, check=True).stdout.strip()


def csv_dir(scale: str) -> Path:
    sf = SCALE_FACTORS[scale]
    d = CACHE / f"sf{sf}"
    if not (d / "lineitem.csv").exists():
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tpchgen-cli", "csv", "-s", sf, f"--output-dir={d}"], check=True)
    return d


def build(admin_dsn: str, dbname: str, scale: str, seed: int) -> str:
    """Create `dbname` and load TPC-H data at the given scale. `seed` is unused:
    TPC-H data is fully determined by the scale factor (the seed is only recorded)."""
    src = csv_dir(scale)
    db.drop_database(admin_dsn, dbname)
    db.create_database(admin_dsn, dbname)
    dsn = db.with_database(admin_dsn, dbname)
    with db.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql())
            for t in TABLES:
                with open(src / f"{t}.csv", "rb") as f, cur.copy(
                    f"COPY {t} FROM STDIN (FORMAT csv, HEADER true)"
                ) as cp:
                    while chunk := f.read(1 << 20):
                        cp.write(chunk)
            for t, cols in PRIMARY_KEYS.items():
                cur.execute(f"ALTER TABLE {t} ADD PRIMARY KEY ({cols})")
            cur.execute("CREATE TABLE cloudpulse_dataset (key text PRIMARY KEY, value text NOT NULL)")
            for k, v in {"name": scale, "seed": str(seed), "generator": generator_version()}.items():
                cur.execute("INSERT INTO cloudpulse_dataset VALUES (%s, %s)", (k, v))
        conn.commit()
    with db.cursor(dsn) as cur:
        for t in TABLES:
            cur.execute(f"VACUUM (FREEZE, ANALYZE) {t}")
    return dsn


def fingerprint(dsn: str) -> dict:
    with db.cursor(dsn) as cur:
        cur.execute("SELECT count(*), sum(l_extendedprice) FROM lineitem")
        n_l, s_l = cur.fetchone()
        cur.execute("SELECT count(*), sum(o_totalprice) FROM orders")
        n_o, s_o = cur.fetchone()
    return {"lineitem": int(n_l), "lineitem_sum": str(s_l), "orders": int(n_o), "orders_sum": str(s_o)}


def code_fingerprint() -> str:
    return schema_sql() + generator_version() + repr(PRIMARY_KEYS)


# ------------------------------------------------------------------ parameters
# Deterministic substitution parameters, generated in SQL after setseed().
ORDER_KEYS = {"type": "sql", "n": 40,
              "query": "SELECT o_orderkey FROM orders ORDER BY random() LIMIT %(n)s"}
CUSTOMERS_WITH_ORDERS = {  # about a third of TPC-H customers never place an order
    "type": "sql", "n": 40,
    "query": "SELECT c FROM (SELECT DISTINCT o_custkey AS c FROM orders ORDER BY 1) s "
             "ORDER BY random() LIMIT %(n)s",
}
Q14_MONTHS = {  # Q14: a random month in 1993..1997 (spec 2.4.14.3)
    "type": "sql", "n": 12,
    "query": "SELECT d, d FROM (SELECT (date '1993-01-01' + make_interval(months => "
             "floor(random() * 60)::int))::date AS d FROM generate_series(1, %(n)s)) s",
}
Q6_PARAMS = {  # Q6: year 1993..1997, discount 0.02..0.09, quantity 24..25 (spec 2.4.6.3)
    "type": "sql", "n": 10,
    "query": "SELECT d, d, disc, disc, qty FROM (SELECT (date '1993-01-01' + make_interval(years => "
             "floor(random() * 5)::int))::date AS d, (2 + floor(random() * 8)) / 100.0 AS disc, "
             "24 + floor(random() * 2)::int AS qty FROM generate_series(1, %(n)s)) s",
}
Q4_QUARTERS = {  # Q4: first day of a random month in 1993-01..1997-10 (spec 2.4.4.3)
    "type": "sql", "n": 10,
    "query": "SELECT d, d FROM (SELECT (date '1993-01-01' + make_interval(months => "
             "floor(random() * 58)::int))::date AS d FROM generate_series(1, %(n)s)) s",
}
Q3_PARAMS = {  # Q3: a market segment and a day in March 1995 (spec 2.4.3.3)
    "type": "sql", "n": 5,
    "query": "SELECT (ARRAY['AUTOMOBILE','BUILDING','FURNITURE','MACHINERY','HOUSEHOLD'])"
             "[1 + floor(random() * 5)::int], d, d FROM (SELECT date '1995-03-01' + "
             "floor(random() * 31)::int AS d FROM generate_series(1, %(n)s)) s",
}

WORKLOAD: list[QuerySpec] = [
    QuerySpec("tpch_order_lookup",
              "SELECT o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate "
              "FROM orders WHERE o_orderkey = %s",
              params=ORDER_KEYS, weight=30),
    QuerySpec("tpch_customer_recent_orders",
              "SELECT o_orderkey, o_totalprice, o_orderdate FROM orders WHERE o_custkey = %s "
              "ORDER BY o_orderdate DESC LIMIT 10",
              params=CUSTOMERS_WITH_ORDERS, weight=20),
    QuerySpec("tpch_order_lines",
              "SELECT l_linenumber, l_partkey, l_quantity, l_extendedprice FROM lineitem "
              "WHERE l_orderkey = %s",
              params=ORDER_KEYS, weight=20),
    QuerySpec("tpch_q14_promotion_effect",
              "SELECT 100.00 * sum(CASE WHEN p_type LIKE 'PROMO%%' "
              "THEN l_extendedprice * (1 - l_discount) ELSE 0 END) "
              "/ sum(l_extendedprice * (1 - l_discount)) AS promo_revenue "
              "FROM lineitem, part WHERE l_partkey = p_partkey "
              "AND l_shipdate >= %s::date AND l_shipdate < %s::date + interval '1 month'",
              params=Q14_MONTHS, weight=2),
    QuerySpec("tpch_q6_forecast_revenue",
              "SELECT sum(l_extendedprice * l_discount) AS revenue FROM lineitem "
              "WHERE l_shipdate >= %s::date AND l_shipdate < %s::date + interval '1 year' "
              "AND l_discount BETWEEN %s - 0.01 AND %s + 0.01 AND l_quantity < %s",
              params=Q6_PARAMS, weight=1),
    QuerySpec("tpch_q4_order_priority",
              "SELECT o_orderpriority, count(*) AS order_count FROM orders "
              "WHERE o_orderdate >= %s::date AND o_orderdate < %s::date + interval '3 months' "
              "AND EXISTS (SELECT 1 FROM lineitem WHERE l_orderkey = o_orderkey "
              "AND l_commitdate < l_receiptdate) "
              "GROUP BY o_orderpriority ORDER BY o_orderpriority",
              params=Q4_QUARTERS, weight=1),
    QuerySpec("tpch_q3_shipping_priority",
              "SELECT l_orderkey, sum(l_extendedprice * (1 - l_discount)) AS revenue, "
              "o_orderdate, o_shippriority FROM customer, orders, lineitem "
              "WHERE c_mktsegment = %s AND c_custkey = o_custkey AND l_orderkey = o_orderkey "
              "AND o_orderdate < %s::date AND l_shipdate > %s::date "
              "GROUP BY l_orderkey, o_orderdate, o_shippriority "
              "ORDER BY revenue DESC, o_orderdate, l_orderkey LIMIT 10",
              params=Q3_PARAMS, weight=0.5),
    QuerySpec("tpch_touch_lineitem",
              "UPDATE lineitem SET l_comment = l_comment WHERE l_orderkey = %s AND l_linenumber = 1",
              kind="write", params=ORDER_KEYS, weight=10),
]

BASELINE_SQL = [
    "CREATE INDEX orders_o_custkey_idx ON orders (o_custkey)",
    "CREATE INDEX orders_o_orderdate_idx ON orders (o_orderdate)",
    "CREATE INDEX lineitem_l_shipdate_idx ON lineitem (l_shipdate)",
    "ANALYZE orders",
    "ANALYZE lineitem",
]
BASELINE_DB_SETTINGS = {"work_mem": "64MB"}

SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario("t0_control", "TPC-H: no fault (false-positive control)",
                 story="Nothing changes; any regression flagged is a false positive."),
        Scenario("t1_shipdate_index_dropped", "TPC-H: index on lineitem.l_shipdate dropped",
                 fault_sql=["DROP INDEX lineitem_l_shipdate_idx"],
                 regressed_query="tpch_q14_promotion_effect",
                 expected_diagnosis="selective_seq_scan", expected_candidate="(l_shipdate)",
                 story="Q14 reads one month of shipments (~1.3% of lineitem). Without the "
                       "index it must scan all of lineitem."),
        Scenario("t2_custkey_index_dropped", "TPC-H: index on orders.o_custkey dropped",
                 fault_sql=["DROP INDEX orders_o_custkey_idx"],
                 regressed_query="tpch_customer_recent_orders",
                 expected_diagnosis="selective_seq_scan", expected_candidate="(o_custkey)",
                 story="'Latest orders of a customer' falls back to scanning all orders."),
        Scenario("t3_orderdate_index_dropped", "TPC-H: index on orders.o_orderdate dropped",
                 fault_sql=["DROP INDEX orders_o_orderdate_idx"],
                 regressed_query="tpch_q4_order_priority",
                 expected_diagnosis="selective_seq_scan", expected_candidate="(o_orderdate)",
                 story="Q4 reads one quarter of orders (~3.8%). Whether the index matters "
                       "enough to be a regression is for the measurements to say."),
    ]
}

TPCH = Pack(
    name="tpch",
    workload=WORKLOAD,
    baseline_sql=BASELINE_SQL,
    baseline_db_settings=BASELINE_DB_SETTINGS,
    scenarios=SCENARIOS,
    tables=TABLES,
    scales=list(SCALE_FACTORS),
    build=build,
    fingerprint=fingerprint,
    code_fingerprint=code_fingerprint,
    noisy_sql="SELECT sum(hashtext(l::text)) FROM lineitem l",
)
