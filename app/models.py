"""Pydantic models describing the snapshot contract sent by the serverStatus agent.

If this validation fails the payload is rejected at the edge (HTTP 422) instead of
being written into the database, so the dashboard never renders a half-valid sample.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Cpu(BaseModel):
    total_pct: float = Field(ge=0, le=100)
    per_core_pct: list[float] = Field(default_factory=list)


class Mem(BaseModel):
    total: int = Field(ge=0)
    used: int = Field(ge=0)
    available: int = Field(ge=0)
    swap_total: int = Field(default=0, ge=0)
    swap_used: int = Field(default=0, ge=0)


class Disk(BaseModel):
    mount: str
    total: int = Field(ge=0)
    used: int = Field(ge=0)


class Net(BaseModel):
    rx_bytes: int = Field(ge=0)
    tx_bytes: int = Field(ge=0)


class DiskIO(BaseModel):
    read_bytes: int = Field(ge=0)
    write_bytes: int = Field(ge=0)


class PathSize(BaseModel):
    path: str
    bytes: int = Field(ge=0)


class DiskSpace(BaseModel):
    scanned_at: int = Field(ge=0)
    roots: list[str] = Field(default_factory=list)
    top_dirs: list[PathSize] = Field(default_factory=list)
    top_files: list[PathSize] = Field(default_factory=list)


class Proc(BaseModel):
    pid: int
    name: str
    cpu_pct: float = Field(ge=0)
    mem_pct: float = Field(ge=0)


class Processes(BaseModel):
    count: int = Field(ge=0)
    top_cpu: list[Proc] = Field(default_factory=list)
    top_mem: list[Proc] = Field(default_factory=list)


class Snapshot(BaseModel):
    ts: int = Field(gt=0, description="Unix seconds, UTC, set by the agent")
    hostname: str = Field(min_length=1)
    uptime_s: int = Field(ge=0)
    load: list[float] = Field(min_length=3, max_length=3, description="[1m, 5m, 15m]")
    cpu: Cpu
    mem: Mem
    disks: list[Disk] = Field(default_factory=list)
    net: Net
    diskio: DiskIO
    disk_space: DiskSpace
    processes: Processes
