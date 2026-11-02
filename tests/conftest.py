"""Shared fixtures.

Tests marked `db` need the three PostgreSQL servers (docker compose up). They use
their own scratch databases (cp_test_*), never the real target/meta databases.
"""

from __future__ import annotations

import dataclasses

import psycopg
import pytest

from cloudpulse import db
from cloudpulse.config import load_settings


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except psycopg.Error:
        return False


@pytest.fixture(scope="session")
def settings():
    s = load_settings()
    if not all(_reachable(d) for d in (s.target_dsn.rsplit("/", 1)[0] + "/postgres",
                                       s.lab_dsn, s.meta_dsn)):
        pytest.skip("PostgreSQL servers not reachable (run: docker compose up -d)")
    return s


@pytest.fixture(scope="session")
def test_meta_dsn(settings):
    from cloudpulse.meta import store
    admin = settings.meta_dsn
    db.drop_database(admin, "cp_test_meta")
    db.create_database(admin, "cp_test_meta")
    dsn = db.with_database(admin, "cp_test_meta")
    store.init_schema(dsn)
    yield dsn
    db.drop_database(admin, "cp_test_meta")


@pytest.fixture(scope="session")
def test_target_dsn(settings):
    """A tiny generated database on the TARGET server, with pg_stat_statements."""
    from cloudpulse.datagen import generate
    admin = db.with_database(settings.target_dsn, "postgres")
    dsn = generate.build_database(admin, "cp_test_target", generate.SCALES["tiny"], 42)
    with db.cursor(dsn) as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    yield dsn
    db.drop_database(admin, "cp_test_target")


@pytest.fixture
def test_settings(settings, test_meta_dsn, test_target_dsn):
    return dataclasses.replace(settings, meta_dsn=test_meta_dsn, target_dsn=test_target_dsn)
