"""SQLite storage: schema, ingest transaction, read queries and retention pruning.

Uses only the stdlib ``sqlite3`` module — no native build step. One connection per
thread (FastAPI runs sync endpoints in a threadpool), WAL mode for cheap concurrent
reads while the agent writes once a minute.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .config import settings
from .models import Snapshot

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  id          INTEGER PRIMARY KEY,
  ts          INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  hostname    TEXT    NOT NULL,
  uptime_s    INTEGER NOT NULL,
  load1       REAL    NOT NULL,
  load5       REAL    NOT NULL,
  load15      REAL    NOT NULL,
  cpu_pct     REAL    NOT NULL,
  cpu_cores   TEXT    NOT NULL,
  mem_total   INTEGER NOT NULL,
  mem_used    INTEGER NOT NULL,
  mem_avail   INTEGER NOT NULL,
  swap_total  INTEGER NOT NULL,
  swap_used   INTEGER NOT NULL,
  net_rx      INTEGER NOT NULL,
  net_tx      INTEGER NOT NULL,
  diskio_r    INTEGER NOT NULL,
  diskio_w    INTEGER NOT NULL,
  proc_count  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

CREATE TABLE IF NOT EXISTS disks (
  sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
  mount     TEXT    NOT NULL,
  total     INTEGER NOT NULL,
  used      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disks_sample ON disks(sample_id);

CREATE TABLE IF NOT EXISTS processes (
  sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
  pid       INTEGER NOT NULL,
  name      TEXT    NOT NULL,
  cpu_pct   REAL    NOT NULL,
  mem_pct   REAL    NOT NULL,
  list      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processes_sample ON processes(sample_id);

CREATE TABLE IF NOT EXISTS disk_scans (
  scanned_at INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS disk_items (
  scanned_at INTEGER NOT NULL REFERENCES disk_scans(scanned_at) ON DELETE CASCADE,
  kind       TEXT    NOT NULL,
  rank       INTEGER NOT NULL,
  path       TEXT    NOT NULL,
  bytes      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disk_items_scan ON disk_items(scanned_at);
"""


def _connect() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


# --------------------------------------------------------------------------- write


def store_snapshot(snap: Snapshot) -> None:
    """Persist one snapshot atomically, then prune old rows."""
    conn = get_conn()
    now = int(time.time())
    with conn:  # single transaction
        cur = conn.execute(
            """
            INSERT INTO samples (
              ts, received_at, hostname, uptime_s, load1, load5, load15,
              cpu_pct, cpu_cores, mem_total, mem_used, mem_avail,
              swap_total, swap_used, net_rx, net_tx, diskio_r, diskio_w, proc_count
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snap.ts,
                now,
                snap.hostname,
                snap.uptime_s,
                float(snap.load[0]),
                float(snap.load[1]),
                float(snap.load[2]),
                float(snap.cpu.total_pct),
                json.dumps(snap.cpu.per_core_pct),
                snap.mem.total,
                snap.mem.used,
                snap.mem.available,
                snap.mem.swap_total,
                snap.mem.swap_used,
                snap.net.rx_bytes,
                snap.net.tx_bytes,
                snap.diskio.read_bytes,
                snap.diskio.write_bytes,
                snap.processes.count,
            ),
        )
        sample_id = int(cur.lastrowid)

        if snap.disks:
            conn.executemany(
                "INSERT INTO disks (sample_id, mount, total, used) VALUES (?,?,?,?)",
                [(sample_id, d.mount, d.total, d.used) for d in snap.disks],
            )

        proc_rows: list[tuple[Any, ...]] = []
        for p in snap.processes.top_cpu:
            proc_rows.append((sample_id, p.pid, p.name, p.cpu_pct, p.mem_pct, "top_cpu"))
        for p in snap.processes.top_mem:
            proc_rows.append((sample_id, p.pid, p.name, p.cpu_pct, p.mem_pct, "top_mem"))
        if proc_rows:
            conn.executemany(
                """
                INSERT INTO processes (sample_id, pid, name, cpu_pct, mem_pct, list)
                VALUES (?,?,?,?,?,?)
                """,
                proc_rows,
            )

        # Disk-space scans are stored once per scan, not per 60s sample.
        scanned_at = snap.disk_space.scanned_at
        if scanned_at > 0 and not conn.execute(
            "SELECT 1 FROM disk_scans WHERE scanned_at = ?", (scanned_at,)
        ).fetchone():
            conn.execute("INSERT INTO disk_scans (scanned_at) VALUES (?)", (scanned_at,))
            items = [
                (scanned_at, "dir", rank, d.path, d.bytes)
                for rank, d in enumerate(snap.disk_space.top_dirs, start=1)
            ]
            items += [
                (scanned_at, "file", rank, f.path, f.bytes)
                for rank, f in enumerate(snap.disk_space.top_files, start=1)
            ]
            if items:
                conn.executemany(
                    "INSERT INTO disk_items (scanned_at, kind, rank, path, bytes) VALUES (?,?,?,?,?)",
                    items,
                )

    prune(conn)


def prune(conn: sqlite3.Connection) -> int:
    """Delete samples/scans outside the retention window. Returns samples deleted."""
    now = int(time.time())
    with conn:
        cur = conn.execute(
            "DELETE FROM samples WHERE ts < ?",
            (now - settings.sample_retention_hours * 3600,),
        )
        deleted = max(0, cur.rowcount)
        conn.execute(
            "DELETE FROM disk_scans WHERE scanned_at < ?",
            (now - settings.scan_retention_days * 86400,),
        )
    return deleted


def prune_now() -> int:
    """Prune from a background thread and fold the WAL back into the main file.

    Called on a timer so retention holds even when the agent stops reporting.
    """
    conn = get_conn()
    deleted = prune(conn)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    return deleted


# ---------------------------------------------------------------------------- read


def _rate(current: int, previous: int | None, dt: int) -> tuple[float | None, bool]:
    """Return (per_second, reset). A negative delta means the box rebooted."""
    if previous is None or dt <= 0:
        return None, False
    delta = current - previous
    if delta < 0:
        return None, True
    return delta / dt, False


def _parse_cores(raw: str) -> list[float]:
    try:
        value = json.loads(raw)
        return [float(x) for x in value]
    except (TypeError, ValueError):
        return []


def get_current() -> dict[str, Any]:
    conn = get_conn()
    latest = conn.execute("SELECT * FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
    if latest is None:
        return {"sample": None, "disk_scan": get_disk(), "stats": get_stats()}

    prev = conn.execute(
        """
        SELECT ts, net_rx, net_tx, diskio_r, diskio_w
        FROM samples WHERE ts < ? ORDER BY ts DESC LIMIT 1
        """,
        (latest["ts"],),
    ).fetchone()

    dt = (latest["ts"] - prev["ts"]) if prev else 0
    rx_per_s, net_reset = _rate(latest["net_rx"], prev["net_rx"] if prev else None, dt)
    tx_per_s, _ = _rate(latest["net_tx"], prev["net_tx"] if prev else None, dt)
    rd_per_s, rd_reset = _rate(latest["diskio_r"], prev["diskio_r"] if prev else None, dt)
    wr_per_s, wr_reset = _rate(latest["diskio_w"], prev["diskio_w"] if prev else None, dt)
    io_reset = rd_reset or wr_reset

    disk_rows = conn.execute(
        "SELECT mount, total, used FROM disks WHERE sample_id = ? ORDER BY mount",
        (latest["id"],),
    ).fetchall()

    proc_rows = conn.execute(
        "SELECT pid, name, cpu_pct, mem_pct, list FROM processes WHERE sample_id = ?",
        (latest["id"],),
    ).fetchall()
    top_cpu = [
        {"pid": r["pid"], "name": r["name"], "cpu_pct": r["cpu_pct"], "mem_pct": r["mem_pct"]}
        for r in proc_rows
        if r["list"] == "top_cpu"
    ]
    top_mem = [
        {"pid": r["pid"], "name": r["name"], "cpu_pct": r["cpu_pct"], "mem_pct": r["mem_pct"]}
        for r in proc_rows
        if r["list"] == "top_mem"
    ]

    now = int(time.time())
    return {
        "sample": {
            "ts": latest["ts"],
            "received_at": latest["received_at"],
            "age_s": max(0, now - latest["ts"]),
            "hostname": latest["hostname"],
            "uptime_s": latest["uptime_s"],
            "load": [latest["load1"], latest["load5"], latest["load15"]],
            "cpu": {
                "total_pct": latest["cpu_pct"],
                "per_core_pct": _parse_cores(latest["cpu_cores"]),
            },
            "mem": {
                "total": latest["mem_total"],
                "used": latest["mem_used"],
                "available": latest["mem_avail"],
                "swap_total": latest["swap_total"],
                "swap_used": latest["swap_used"],
            },
            "disks": [
                {"mount": r["mount"], "total": r["total"], "used": r["used"]} for r in disk_rows
            ],
            "net": {
                "rx_bytes": latest["net_rx"],
                "tx_bytes": latest["net_tx"],
                "rx_per_s": rx_per_s,
                "tx_per_s": tx_per_s,
                "reset": net_reset,
            },
            "diskio": {
                "read_bytes": latest["diskio_r"],
                "write_bytes": latest["diskio_w"],
                "read_per_s": rd_per_s,
                "write_per_s": wr_per_s,
                "reset": io_reset,
            },
            "processes": {
                "count": latest["proc_count"],
                "top_cpu": top_cpu,
                "top_mem": top_mem,
            },
        },
        "disk_scan": get_disk(),
        "stats": get_stats(),
    }


MAX_POINTS = 600


def _downsample(points: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(points) <= limit:
        return points
    bucket = len(points) / limit
    out: list[dict[str, Any]] = []
    for i in range(limit):
        chunk = points[int(i * bucket) : int((i + 1) * bucket)]
        if not chunk:
            continue
        merged: dict[str, Any] = {"ts": chunk[-1]["ts"]}
        for key in ("cpu", "mem", "rx", "tx"):
            vals = [c[key] for c in chunk if c.get(key) is not None]
            merged[key] = round(sum(vals) / len(vals), 2) if vals else None
        out.append(merged)
    return out


def get_history(range_seconds: int) -> dict[str, Any]:
    conn = get_conn()
    cutoff = int(time.time()) - range_seconds
    rows = conn.execute(
        """
        SELECT ts, cpu_pct, mem_used, mem_total, net_rx, net_tx
        FROM samples WHERE ts >= ? ORDER BY ts ASC
        """,
        (cutoff,),
    ).fetchall()

    points: list[dict[str, Any]] = []
    prev: sqlite3.Row | None = None
    for r in rows:
        mem_pct = (r["mem_used"] / r["mem_total"] * 100.0) if r["mem_total"] else 0.0
        rx = tx = None
        if prev is not None:
            dt = r["ts"] - prev["ts"]
            if dt > 0:
                drx, _ = _rate(r["net_rx"], prev["net_rx"], dt)
                dtx, _ = _rate(r["net_tx"], prev["net_tx"], dt)
                rx, tx = drx, dtx
        points.append(
            {
                "ts": r["ts"],
                "cpu": round(r["cpu_pct"], 2),
                "mem": round(mem_pct, 2),
                "rx": rx,
                "tx": tx,
            }
        )
        prev = r

    return {"range_s": range_seconds, "points": _downsample(points, MAX_POINTS)}


def get_disk() -> dict[str, Any] | None:
    conn = get_conn()
    row = conn.execute("SELECT scanned_at FROM disk_scans ORDER BY scanned_at DESC LIMIT 1").fetchone()
    if row is None:
        return None
    items = conn.execute(
        "SELECT kind, rank, path, bytes FROM disk_items WHERE scanned_at = ? ORDER BY kind, rank",
        (row["scanned_at"],),
    ).fetchall()
    return {
        "scanned_at": row["scanned_at"],
        "top_dirs": [{"path": r["path"], "bytes": r["bytes"]} for r in items if r["kind"] == "dir"],
        "top_files": [
            {"path": r["path"], "bytes": r["bytes"]} for r in items if r["kind"] == "file"
        ],
    }


def get_stats() -> dict[str, Any]:
    conn = get_conn()
    counts = conn.execute("SELECT COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last FROM samples").fetchone()
    recent = conn.execute("SELECT ts FROM samples ORDER BY ts DESC LIMIT 20").fetchall()
    cadence = None
    if len(recent) >= 2:
        diffs = [recent[i]["ts"] - recent[i + 1]["ts"] for i in range(len(recent) - 1)]
        diffs = [d for d in diffs if d > 0]
        if diffs:
            cadence = round(sum(diffs) / len(diffs), 1)
    host = conn.execute("SELECT hostname FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
    db_bytes = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    return {
        "samples": counts["n"],
        "first_ts": counts["first"],
        "last_ts": counts["last"],
        "cadence_s": cadence,
        "hostname": host["hostname"] if host else None,
        "db_bytes": db_bytes,
        "retention_hours": settings.sample_retention_hours,
        "scan_retention_days": settings.scan_retention_days,
    }
