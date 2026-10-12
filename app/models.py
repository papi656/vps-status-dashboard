"""Pydantic models describing the snapshot contract sent by the serverStatus agent.

If this validation fails the payload is rejected at the edge (HTTP 422) instead of
being written into the database, so the dashboard never renders a half-valid sample.

The bounds here are also a defence: every collection is length-capped and every
timestamp is sanity-checked, so a single authenticated request cannot write an
unbounded number of rows (or rows that retention would never be able to prune).
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field, model_validator

from .config import settings

# Loose upper bounds — comfortably above anything a real machine reports, tight
# enough that one request can't bloat the database.
MAX_CORES = 1024
MAX_MOUNTS = 256
MAX_SCAN_ITEMS = 100
MAX_PROCESSES = 100
MAX_PATH_LEN = 4096
MAX_NAME_LEN = 256
MAX_HOSTNAME_LEN = 255


class Cpu(BaseModel):
    total_pct: float = Field(ge=0, le=100)
    per_core_pct: list[float] = Field(default_factory=list, max_length=MAX_CORES)


class Mem(BaseModel):
    total: int = Field(ge=0)
    used: int = Field(ge=0)
    available: int = Field(ge=0)
    swap_total: int = Field(default=0, ge=0)
    swap_used: int = Field(default=0, ge=0)


class Disk(BaseModel):
    mount: str = Field(max_length=MAX_NAME_LEN)
    total: int = Field(ge=0)
    used: int = Field(ge=0)


class Net(BaseModel):
    rx_bytes: int = Field(ge=0)
    tx_bytes: int = Field(ge=0)


class DiskIO(BaseModel):
    read_bytes: int = Field(ge=0)
    write_bytes: int = Field(ge=0)


class PathSize(BaseModel):
    path: str = Field(max_length=MAX_PATH_LEN)
    bytes: int = Field(ge=0)


class DiskSpace(BaseModel):
    scanned_at: int = Field(ge=0)
    roots: list[str] = Field(default_factory=list, max_length=MAX_SCAN_ITEMS)
    top_dirs: list[PathSize] = Field(default_factory=list, max_length=MAX_SCAN_ITEMS)
    top_files: list[PathSize] = Field(default_factory=list, max_length=MAX_SCAN_ITEMS)


class Proc(BaseModel):
    pid: int
    name: str = Field(max_length=MAX_NAME_LEN)
    cpu_pct: float = Field(ge=0)
    mem_pct: float = Field(ge=0)


class Processes(BaseModel):
    count: int = Field(ge=0)
    top_cpu: list[Proc] = Field(default_factory=list, max_length=MAX_PROCESSES)
    top_mem: list[Proc] = Field(default_factory=list, max_length=MAX_PROCESSES)


class Snapshot(BaseModel):
    ts: int = Field(gt=0, description="Unix seconds, UTC, set by the agent")
    hostname: str = Field(min_length=1, max_length=MAX_HOSTNAME_LEN)
    uptime_s: int = Field(ge=0)
    load: list[float] = Field(min_length=3, max_length=3, description="[1m, 5m, 15m]")
    cpu: Cpu
    mem: Mem
    disks: list[Disk] = Field(default_factory=list, max_length=MAX_MOUNTS)
    net: Net
    diskio: DiskIO
    disk_space: DiskSpace
    processes: Processes

    @model_validator(mode="after")
    def _check_timestamps(self) -> "Snapshot":
        """Reject timestamps that would defeat retention or are obviously wrong.

        A future ``ts`` matters most: retention prunes `ts < now - window`, so a row
        dated years ahead would never be deleted. That is a slow disk-fill attack
        with a single valid token, so it is refused here.
        """
        now = int(time.time())
        skew = settings.clock_skew_seconds

        if self.ts > now + skew:
            raise ValueError(
                f"ts is {self.ts - now}s in the future (max clock skew {skew}s)"
            )
        if self.ts < now - (settings.sample_retention_hours * 3600 + 3600):
            raise ValueError(
                f"ts is older than the {settings.sample_retention_hours}h retention window"
            )
        if self.disk_space.scanned_at > now + skew:
            raise ValueError(
                "disk_space.scanned_at is in the future "
                f"(max clock skew {skew}s)"
            )
        return self
