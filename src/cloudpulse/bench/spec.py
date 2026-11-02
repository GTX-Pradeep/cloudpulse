"""Experiment specification: what to run, on which arms, with which protocol."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class QuerySpec:
    name: str
    sql: str  # psycopg placeholders (%s); sent with server-side binding ($1, $2, ...)
    kind: str = "read"  # "read" or "write"
    params: dict[str, Any] = field(default_factory=lambda: {"type": "none"})
    weight: float = 1.0  # relative frequency in the tracked workload
    compare_results: bool = True

    def __post_init__(self) -> None:
        if self.kind not in ("read", "write"):
            raise ValueError(f"query {self.name}: kind must be read or write")


@dataclass
class ArmSpec:
    name: str
    setup_sql: list[str] = field(default_factory=list)
    session: dict[str, str] = field(default_factory=dict)  # e.g. {"work_mem": "64MB"}


@dataclass
class Protocol:
    blocks: int = 20
    warmup_rounds: int = 1
    seed: int = 1234
    statement_timeout_ms: int = 60_000
    # Applied to every lab session. synchronous_commit=off removes fsync noise
    # from write measurements (D-009); index-maintenance work is still measured.
    lab_session: dict[str, str] = field(
        default_factory=lambda: {"synchronous_commit": "off"}
    )


@dataclass
class ExperimentSpec:
    name: str
    description: str
    scale: str
    seed: int
    baseline_sql: list[str]
    queries: list[QuerySpec]
    arm_a: ArmSpec
    arm_b: ArmSpec
    protocol: Protocol = field(default_factory=Protocol)
    target_query: str | None = None  # name of the query the candidate is meant to fix

    @staticmethod
    def from_dict(d: dict[str, Any]) -> ExperimentSpec:
        return ExperimentSpec(
            name=d["name"],
            description=d.get("description", ""),
            scale=d.get("scale", "default"),
            seed=int(d.get("seed", 42)),
            baseline_sql=list(d.get("baseline_sql", [])),
            queries=[QuerySpec(**q) for q in d["queries"]],
            arm_a=ArmSpec(**{"name": "A", **d.get("arm_a", {})}),
            arm_b=ArmSpec(**{"name": "B", **d.get("arm_b", {})}),
            protocol=Protocol(**d.get("protocol", {})),
            target_query=d.get("target_query"),
        )

    @staticmethod
    def load(path: str | Path) -> ExperimentSpec:
        return ExperimentSpec.from_dict(json.loads(Path(path).read_text()))
