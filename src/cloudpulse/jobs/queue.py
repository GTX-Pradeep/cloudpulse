"""A PostgreSQL-backed job queue.

Why not Kafka/Redis: we run a handful of long jobs (minutes each), not millions
of small messages. PostgreSQL already gives us durable storage, transactions and
row locks, which is all a reliable queue needs here (D-017).

Claiming uses `FOR UPDATE SKIP LOCKED`: concurrent workers each lock a different
row instead of waiting on (or double-claiming) the same one. A claimed job gets a
lease. If the worker crashes, nobody renews the lease; once it expires the job
becomes claimable again, until max_attempts is reached.
"""

from __future__ import annotations

import os
import socket
from typing import Any

from cloudpulse.meta import store

LEASE_SECONDS = 120

CLAIM_SQL = """
UPDATE jobs SET status = 'running', locked_by = %(worker)s, attempts = attempts + 1,
       lease_until = now() + make_interval(secs => %(lease)s), updated_at = now()
WHERE id = (
    SELECT id FROM jobs
    WHERE attempts < max_attempts
      AND (status = 'queued' OR (status = 'running' AND lease_until < now()))
    ORDER BY id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id, kind, payload, attempts
"""


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def enqueue(meta_dsn: str, kind: str, payload: dict[str, Any], max_attempts: int = 3) -> int:
    return store.execute(
        meta_dsn,
        "INSERT INTO jobs (kind, payload, max_attempts) VALUES (%s, %s, %s) RETURNING id",
        (kind, store.jsonb(payload), max_attempts),
    )[0]


def claim(meta_dsn: str, worker: str, lease_seconds: int = LEASE_SECONDS) -> dict | None:
    return store.fetch_one(meta_dsn, CLAIM_SQL, {"worker": worker, "lease": lease_seconds})


def heartbeat(meta_dsn: str, job_id: int, worker: str, lease_seconds: int = LEASE_SECONDS) -> bool:
    """Extend the lease. Returns False if the job is no longer ours (lease lost)."""
    row = store.execute(
        meta_dsn,
        "UPDATE jobs SET lease_until = now() + make_interval(secs => %s), updated_at = now() "
        "WHERE id = %s AND locked_by = %s AND status = 'running' RETURNING id",
        (lease_seconds, job_id, worker),
    )
    return row is not None


def complete(meta_dsn: str, job_id: int, worker: str) -> None:
    store.execute(
        meta_dsn,
        "UPDATE jobs SET status = 'done', lease_until = NULL, updated_at = now() "
        "WHERE id = %s AND locked_by = %s",
        (job_id, worker),
    )


def fail(meta_dsn: str, job_id: int, worker: str, error: str) -> None:
    store.execute(
        meta_dsn,
        "UPDATE jobs SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END, "
        "last_error = %s, lease_until = NULL, locked_by = NULL, updated_at = now() "
        "WHERE id = %s AND locked_by = %s",
        (error[:2000], job_id, worker),
    )


def sweep(meta_dsn: str) -> int:
    """Mark jobs whose lease expired on their last allowed attempt as failed."""
    rows = store.fetch_all(
        meta_dsn,
        "UPDATE jobs SET status = 'failed', last_error = coalesce(last_error, 'lease expired'), "
        "updated_at = now() WHERE status = 'running' AND lease_until < now() "
        "AND attempts >= max_attempts RETURNING id",
    )
    return len(rows)
