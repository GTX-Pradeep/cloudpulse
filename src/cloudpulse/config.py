"""Runtime configuration, read from environment variables (or a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env vars win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    target_dsn: str
    lab_dsn: str
    meta_dsn: str
    env_label: str

    def server_identity(self, dsn: str) -> tuple[str, int]:
        u = urlparse(dsn)
        host = u.hostname or "localhost"
        if host in ("127.0.0.1", "::1"):
            host = "localhost"
        return host, u.port or 5432


def load_settings() -> Settings:
    _load_dotenv(REPO_ROOT / ".env")
    return Settings(
        target_dsn=os.environ.get(
            "CP_TARGET_DSN", "postgresql://cloudpulse@localhost:5433/target"
        ),
        lab_dsn=os.environ.get("CP_LAB_DSN", "postgresql://cloudpulse@localhost:5434/lab"),
        meta_dsn=os.environ.get(
            "CP_META_DSN", "postgresql://cloudpulse@localhost:5435/cloudpulse"
        ),
        env_label=os.environ.get("CP_ENV_LABEL", "unlabelled"),
    )
