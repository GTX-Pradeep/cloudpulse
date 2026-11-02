"""Connection helpers and database-level admin operations."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg import sql
from psycopg.types.numeric import Int8Dumper

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class SafetyError(RuntimeError):
    """Raised when an operation would touch a database it must never touch."""


def connect(dsn: str, autocommit: bool = True) -> psycopg.Connection:
    """Open a connection with settings the benchmarks rely on.

    prepare_threshold=None disables psycopg's automatic server-side prepared
    statements. Otherwise, after 5 executions PostgreSQL may switch to a generic
    plan and silently change what we are measuring (see decisions D-006).
    """
    conn = psycopg.connect(
        dsn, autocommit=autocommit, prepare_threshold=None, options="-c TimeZone=UTC"
    )
    # psycopg sends a Python int as int2/int4/int8 depending on its *value*.
    # pg_stat_statements' queryid includes parameter types, so one query would be
    # split into several entries (id=5 vs id=500000). Always send int8 (D-019).
    conn.adapters.register_dumper(int, Int8Dumper)
    return conn


def with_database(dsn: str, dbname: str) -> str:
    """Return the same DSN pointing at another database on the same server."""
    check_ident(dbname)
    u = urlparse(dsn)
    return urlunparse(u._replace(path="/" + dbname))


def check_ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


@contextmanager
def cursor(dsn: str) -> Iterator[psycopg.Cursor]:
    with connect(dsn) as conn, conn.cursor() as cur:
        yield cur


def database_exists(admin_dsn: str, dbname: str) -> bool:
    with cursor(admin_dsn) as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        return cur.fetchone() is not None


def drop_database(admin_dsn: str, dbname: str) -> None:
    check_ident(dbname)
    with cursor(admin_dsn) as cur:
        cur.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
        )


def create_database(admin_dsn: str, dbname: str, template: str | None = None) -> None:
    """Create a database, optionally as a byte-for-byte copy of a template.

    A template copy (STRATEGY FILE_COPY) duplicates the physical files, so the
    copy has the same row layout, visibility map and planner statistics as the
    template. That is what makes twin experiment databases comparable.
    """
    check_ident(dbname)
    with cursor(admin_dsn) as cur:
        if template is None:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
        else:
            check_ident(template)
            cur.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {} STRATEGY FILE_COPY").format(
                    sql.Identifier(dbname), sql.Identifier(template)
                )
            )


def server_version(dsn: str) -> str:
    with cursor(dsn) as cur:
        cur.execute("SHOW server_version")
        return cur.fetchone()[0]


def assert_not_target(settings, dsn: str) -> None:
    """Refuse to run experiments against the monitored target server."""
    if settings.server_identity(dsn) == settings.server_identity(settings.target_dsn):
        raise SafetyError(
            "refusing to run an experiment on the monitored target server "
            f"{settings.server_identity(dsn)}; experiments must use the lab server"
        )
