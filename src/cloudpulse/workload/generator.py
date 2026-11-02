"""Workload generator: replays the tracked workload against the target.

Load is generated in *rounds*. Each round runs every template a fixed number of
times proportional to its weight (at least once), shuffled and split across a
few concurrent connections. Telemetry is snapshotted after each round.

Why rounds instead of wall-clock intervals: on a laptop a scenario must finish in
minutes, and a rare-but-expensive report query must appear in every interval or
the detector has nothing to compare. A real deployment would snapshot on a timer
(e.g. every 60 s); the collector and detector do not care which (D-014).
"""

from __future__ import annotations

import random
import threading
import time

import psycopg

from cloudpulse import db
from cloudpulse.bench import params as paramsmod
from cloudpulse.bench.spec import QuerySpec


class WorkloadRunner:
    def __init__(self, target_dsn: str, templates: list[QuerySpec], scale: float = 0.5,
                 concurrency: int = 2, seed: int = 7):
        self.dsn = target_dsn
        self.templates = templates
        self.scale = scale
        self.concurrency = concurrency
        self.rng = random.Random(seed)
        self.params = {q.name: paramsmod.sample(q.params, target_dsn, seed + i)
                       for i, q in enumerate(templates)}
        self.errors = 0

    def schedule(self) -> list[tuple[QuerySpec, tuple]]:
        items = []
        for q in self.templates:
            n = max(1, round(q.weight * self.scale))
            for _ in range(n):
                items.append((q, self.rng.choice(self.params[q.name])))
        self.rng.shuffle(items)
        return items

    def _worker(self, items: list[tuple[QuerySpec, tuple]]) -> None:
        # A fresh connection per round, so ALTER DATABASE ... SET changes apply,
        # as they would to an application's newly opened connections.
        with db.connect(self.dsn) as conn:
            for q, p in items:
                try:
                    with conn.cursor() as cur:
                        cur.execute(q.sql, p)
                        if cur.description is not None:
                            cur.fetchall()
                except psycopg.Error:
                    self.errors += 1

    def run_round(self) -> float:
        items = self.schedule()
        chunks = [items[i:: self.concurrency] for i in range(self.concurrency)]
        threads = [threading.Thread(target=self._worker, args=(c,)) for c in chunks]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return time.perf_counter() - t0


class NoisyNeighbor:
    """Background CPU/IO load on the target from queries that are not tracked."""

    SQL = "SELECT sum(hashtext(o::text)) FROM orders o"

    def __init__(self, target_dsn: str, threads: int = 2):
        self.dsn, self.n = target_dsn, threads
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _loop(self) -> None:
        with db.connect(self.dsn) as conn:
            while not self._stop.is_set():
                with conn.cursor() as cur:
                    cur.execute(self.SQL)
                    cur.fetchall()

    def start(self) -> None:
        self._threads = [threading.Thread(target=self._loop, daemon=True) for _ in range(self.n)]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=30)
