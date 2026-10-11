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
    port: int = field(default_factory=lambda: _int("PORT", 8099))

    ingest_token: str = field(default_factory=lambda: os.environ.get("INGEST_TOKEN", "dev"))

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

    public_dir: Path = field(default_factory=lambda: ROOT / "public")


settings = Settings()
