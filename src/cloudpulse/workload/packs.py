"""Registry of workload packs. Template and scenario names are unique across packs."""

from __future__ import annotations

from functools import cache

from cloudpulse.bench.spec import QuerySpec
from cloudpulse.workload.pack import Pack, Scenario


@cache
def all_packs() -> dict[str, Pack]:
    from cloudpulse.workload.suite import ECOMMERCE
    from cloudpulse.workload.tpch import TPCH
    packs = {p.name: p for p in (ECOMMERCE, TPCH)}
    names = [q.name for p in packs.values() for q in p.workload]
    scen = [s for p in packs.values() for s in p.scenarios]
    assert len(names) == len(set(names)), "template names must be unique across packs"
    assert len(scen) == len(set(scen)), "scenario names must be unique across packs"
    return packs


def get(name: str) -> Pack:
    return all_packs()[name]


def template(name: str) -> QuerySpec:
    for p in all_packs().values():
        if name in p.by_name:
            return p.by_name[name]
    raise KeyError(name)


def pack_of_template(name: str) -> Pack:
    for p in all_packs().values():
        if name in p.by_name:
            return p
    raise KeyError(name)


def scenario(name: str) -> tuple[Pack, Scenario]:
    for p in all_packs().values():
        if name in p.scenarios:
            return p, p.scenarios[name]
    raise KeyError(name)
