"""Runtime configuration, read from the environment (optionally via .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader — no python-dotenv dependency."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    host: str = field(default_factory=lambda: os.environ.get("HOST", "0.0.0.0"))
    # Public surface: UI + read-only API. This is the port the reverse proxy routes.
    # Deliberately NOT called PORT: hosting platforms (Coolify included) inject a
    # generic PORT for their own routing, and honouring it silently moved this
    # listener off the port the Dockerfile healthcheck probes.
    public_port: int = field(default_factory=lambda: _int("PUBLIC_PORT", 8098))
    # Ingest surface: /ingest only. Published to host loopback, never routed publicly.
    ingest_port: int = field(default_factory=lambda: _int("INGEST_PORT", 8099))

    # Interactive API docs. Off by default: they advertise the ingest contract.
    enable_docs: bool = field(
        default_factory=lambda: os.environ.get("ENABLE_DOCS", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )

    ingest_token: str = field(default_factory=lambda: os.environ.get("INGEST_TOKEN", "dev"))

    # Minimum seconds between accepted ingests. 30s is 2x the documented 60s cadence;
    # set to 0 to disable (useful when seeding with scripts/replay.py).
    ingest_rate_limit_seconds: int = field(
        default_factory=lambda: _int("INGEST_RATE_LIMIT_SECONDS", 30)
    )
    # Reject ingest bodies larger than this before parsing them.
    max_body_bytes: int = field(
        default_factory=lambda: _int("MAX_INGEST_BODY_BYTES", 1_048_576)
    )
    # Allowed clock skew between the agent and this host, in seconds.
    clock_skew_seconds: int = field(default_factory=lambda: _int("CLOCK_SKEW_SECONDS", 300))

    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("DATA_DIR", ROOT / "data")).resolve()
    )
    db_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("DB_PATH", "")
        ).resolve()
        if os.environ.get("DB_PATH")
        else (Path(os.environ.get("DATA_DIR", ROOT / "data")).resolve() / "status.db")
    )

    sample_retention_hours: int = field(
        default_factory=lambda: _int("SAMPLE_RETENTION_HOURS", 24)
    )
    scan_retention_days: int = field(default_factory=lambda: _int("SCAN_RETENTION_DAYS", 7))
    # How often the background task enforces retention, independent of ingest.
    prune_interval_seconds: int = field(default_factory=lambda: _int("PRUNE_INTERVAL_SECONDS", 300))

    public_dir: Path = field(default_factory=lambda: ROOT / "public")


settings = Settings()
