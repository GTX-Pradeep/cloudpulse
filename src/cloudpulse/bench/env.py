"""Capture the environment a measurement was taken in.

A latency number without its context (hardware, PostgreSQL version, settings,
dataset) cannot be reproduced or compared, so every results file carries this.
"""

from __future__ import annotations

import os
import platform
import subprocess
from datetime import UTC, datetime

import psycopg

from cloudpulse import __version__, db
from cloudpulse.config import REPO_ROOT

SETTINGS = [
    "server_version",
    "shared_buffers",
    "effective_cache_size",
    "work_mem",
    "random_page_cost",
    "jit",
    "max_parallel_workers_per_gather",
    "synchronous_commit",
    "track_io_timing",
    "compute_query_id",
]


def _cpu_model() -> str:
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _mem_total_gb() -> float | None:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal"):
                return round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    return None


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        dirty = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out.stdout.strip() + ("-dirty" if dirty else "") if out.returncode == 0 else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def server_settings(dsn: str) -> dict:
    with db.cursor(dsn) as cur:
        cur.execute(
            "SELECT name, setting || coalesce(unit, '') FROM pg_settings WHERE name = ANY(%s)",
            (SETTINGS,),
        )
        out = dict(cur.fetchall())
        cur.execute("SELECT extname, extversion FROM pg_extension ORDER BY 1")
        out["extensions"] = dict(cur.fetchall())
    return out


def capture(dsn: str, env_label: str) -> dict:
    return {
        "captured_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "env_label": env_label,
        "cloudpulse_version": __version__,
        "git_commit": git_commit(),
        "host": {
            "os": f"{platform.system()} {platform.release()}",
            "cpu_model": _cpu_model(),
            "cpu_count": os.cpu_count(),
            "mem_total_gb": _mem_total_gb(),
            "python": platform.python_version(),
            "psycopg": psycopg.__version__,
        },
        "postgres": server_settings(dsn),
    }
