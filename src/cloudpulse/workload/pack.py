"""A workload pack: everything that is specific to one database and application.

CloudPulse's engine (telemetry, detection, plans, diagnosis, HypoPG, the A/B
harness, statistics, guardrail) knows nothing about any particular schema. A
pack supplies the schema and data, the tracked query templates, the healthy
baseline (indexes and settings) and the faults used for evaluation.

Two packs ship with the project:
  * ecommerce  synthetic customers/orders data designed with known skew (suite.py)
  * tpch       the industry-standard TPC-H benchmark schema and data (tpch.py),
               used to test CloudPulse on a workload it was not designed around
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from cloudpulse.bench.spec import QuerySpec


@dataclass
class Scenario:
    name: str
    title: str
    fault_sql: list[str] = field(default_factory=list)
    fault_db_settings: dict[str, str] = field(default_factory=dict)
    noisy_neighbor: bool = False
    regressed_query: str | None = None
    expected_diagnosis: str | None = None  # diagnosis rule that should explain it
    expected_candidate: str | None = None  # substring of the right candidate
    story: str = ""


@dataclass
class Pack:
    name: str
    workload: list[QuerySpec]
    baseline_sql: list[str]
    baseline_db_settings: dict[str, str]
    scenarios: dict[str, Scenario]
    tables: list[str]
    scales: list[str]
    # (admin_dsn, dbname, scale, seed) -> dsn of a freshly built, frozen database
    build: Callable[[str, str, str, int], str]
    # dsn -> content fingerprint proving two databases hold the same data
    fingerprint: Callable[[str], dict]
    # text that changes whenever the data generator changes (part of the lab base key)
    code_fingerprint: Callable[[], str]
    noisy_sql: str

    @property
    def by_name(self) -> dict[str, QuerySpec]:
        return {q.name: q for q in self.workload}
