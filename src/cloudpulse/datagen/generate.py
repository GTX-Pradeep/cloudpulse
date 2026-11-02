"""Deterministic, skewed synthetic data generation.

The same (scale, seed) always produces the same rows. Generation happens inside
PostgreSQL (generate_series + random() after setseed) so millions of rows load in
seconds, and it runs in a single session so the random sequence is reproducible.

Skew built in on purpose (each one creates a known performance situation):
  * customer popularity follows a power law: the top 1% of customers own ~20% of
    orders, so "orders for customer X" ranges from a handful of rows to thousands;
  * every customer has an activity window, so some heavy customers only have old
    orders (time and customer are correlated, which the planner assumes they are not);
  * order status depends on age: almost everything is 'delivered', and only very
    recent orders are 'pending' or 'shipped' (a highly selective predicate);
  * orders are inserted in created_at order, as a real append-only table would be.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib import resources

from cloudpulse import db

# Fixed reference clock. Queries use these constants instead of now(), so results
# do not change depending on when the benchmark is run.
EPOCH_START = "2024-01-01 00:00:00+00"
EPOCH_END = "2026-01-01 00:00:00+00"


@dataclass(frozen=True)
class Scale:
    name: str
    customers: int
    orders: int
    skew: float = 3.0  # exponent of the power-law customer draw


SCALES = {
    "tiny": Scale("tiny", customers=2_000, orders=40_000),
    "small": Scale("small", customers=20_000, orders=400_000),
    "default": Scale("default", customers=100_000, orders=2_000_000),
}


def _seed_value(seed: int) -> float:
    # setseed() takes a float in [-1, 1]
    return (seed % 1_000_003) / 1_000_003.0


GENERATE_CUSTOMERS = """
INSERT INTO customers (id, name, country, segment, signup_at, last_active_at)
SELECT g,
       'customer_' || g,
       (ARRAY['IN','US','GB','DE','BR','JP','SG','FR'])[1 + floor(r1 * 8)::int],
       CASE WHEN r2 < 0.05 THEN 'enterprise' WHEN r2 < 0.30 THEN 'business' ELSE 'consumer' END,
       s,
       least(s + interval '30 days' + (r4 ^ 2) * interval '700 days', %(end)s::timestamptz)
FROM (
    SELECT g, random() AS r1, random() AS r2,
           %(start)s::timestamptz + random() * (interval '700 days') AS s,
           random() AS r4
    FROM generate_series(1, %(customers)s) AS g
) d
"""

GENERATE_ORDERS = """
INSERT INTO orders (id, customer_id, status, amount, created_at, updated_at)
SELECT row_number() OVER (ORDER BY created_at, customer_id, rs),
       customer_id,
       CASE
         WHEN %(end)s::timestamptz - created_at < interval '2 days'
              THEN CASE WHEN rs < 0.7 THEN 'pending' ELSE 'shipped' END
         WHEN %(end)s::timestamptz - created_at < interval '7 days'
              THEN CASE WHEN rs < 0.8 THEN 'shipped' ELSE 'delivered' END
         ELSE CASE WHEN rs < 0.95 THEN 'delivered'
                   WHEN rs < 0.99 THEN 'cancelled' ELSE 'returned' END
       END,
       round((5 + ra * ra * 495)::numeric, 2),
       created_at,
       created_at + interval '1 hour'
FROM (
    SELECT d.customer_id, d.rs, d.ra,
           c.signup_at + d.rt * (c.last_active_at - c.signup_at) AS created_at
    FROM (
        SELECT 1 + floor(%(customers)s * (random() ^ %(skew)s))::bigint AS customer_id,
               random() AS rt, random() AS rs, random() AS ra
        FROM generate_series(1, %(orders)s)
    ) d
    JOIN customers c ON c.id = d.customer_id
) x
"""


def load_schema_sql() -> str:
    return resources.files("cloudpulse.datagen").joinpath("schema.sql").read_text()


def populate(dsn: str, scale: Scale, seed: int) -> None:
    """Create the schema and generate data into an existing, empty database."""
    params = {
        "customers": scale.customers,
        "orders": scale.orders,
        "skew": scale.skew,
        "start": EPOCH_START,
        "end": EPOCH_END,
    }
    with db.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TimeZone = 'UTC'")
            cur.execute("SET max_parallel_workers_per_gather = 0")  # keep it single-process
            cur.execute(load_schema_sql())
            cur.execute("SELECT setseed(%s)", (_seed_value(seed),))
            cur.execute(GENERATE_CUSTOMERS, params)
            cur.execute(GENERATE_ORDERS, params)
            cur.execute(
                "ALTER TABLE orders ADD CONSTRAINT orders_customer_fk "
                "FOREIGN KEY (customer_id) REFERENCES customers(id)"
            )
            cur.execute(
                "CREATE TABLE cloudpulse_dataset (key text PRIMARY KEY, value text NOT NULL)"
            )
            for k, v in {**asdict(scale), "seed": seed}.items():
                cur.execute("INSERT INTO cloudpulse_dataset VALUES (%s, %s)", (k, str(v)))
        conn.commit()
    # VACUUM cannot run inside a transaction block. FREEZE sets hint bits and the
    # visibility map now, so the first benchmark reads do not pay for that work.
    with db.cursor(dsn) as cur:
        cur.execute("VACUUM (FREEZE, ANALYZE) customers")
        cur.execute("VACUUM (FREEZE, ANALYZE) orders")


def build_database(admin_dsn: str, dbname: str, scale: Scale, seed: int) -> str:
    """(Re)create `dbname` on the server behind admin_dsn and fill it. Returns its DSN."""
    db.drop_database(admin_dsn, dbname)
    db.create_database(admin_dsn, dbname)
    dsn = db.with_database(admin_dsn, dbname)
    populate(dsn, scale, seed)
    return dsn


def fingerprint(dsn: str) -> dict:
    """Content fingerprint used to prove two databases hold identical data."""
    with db.cursor(dsn) as cur:
        cur.execute("SET TimeZone = 'UTC'")
        cur.execute(
            "SELECT count(*), sum(hashtextextended(c::text, 0)::numeric) FROM customers c"
        )
        c_count, c_hash = cur.fetchone()
        cur.execute(
            "SELECT count(*), sum(hashtextextended(o::text, 0)::numeric) FROM orders o"
        )
        o_count, o_hash = cur.fetchone()
    return {
        "customers": int(c_count),
        "customers_hash": str(c_hash),
        "orders": int(o_count),
        "orders_hash": str(o_hash),
    }
