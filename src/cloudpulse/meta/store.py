"""Small data-access layer for the metadata (evidence) database."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from cloudpulse import db


def schema_sql() -> str:
    return resources.files("cloudpulse.meta").joinpath("schema.sql").read_text()


def init_schema(meta_dsn: str, reset: bool = False) -> None:
    with db.connect(meta_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            if reset:
                cur.execute(
                    "DROP TABLE IF EXISTS query_embeddings, jobs, verdicts, experiment_runs, "
                    "experiments, candidates, diagnoses, execution_plans, performance_incidents, "
                    "query_stats_snapshots, stat_snapshots, queries CASCADE"
                )
            cur.execute(schema_sql())
        conn.commit()


def fetch_all(meta_dsn: str, sql: str, params: Any = None) -> list[dict]:
    with db.connect(meta_dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(meta_dsn: str, sql: str, params: Any = None) -> dict | None:
    rows = fetch_all(meta_dsn, sql, params)
    return rows[0] if rows else None


def execute(meta_dsn: str, sql: str, params: Any = None) -> Any:
    with db.connect(meta_dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone() if cur.description else None


def jsonb(v: Any) -> Jsonb:
    return Jsonb(json.loads(json.dumps(v, default=str)))
