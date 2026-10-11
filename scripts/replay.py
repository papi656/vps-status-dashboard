#!/usr/bin/env python3
"""Post synthetic snapshots to the ingest endpoint for local UI development.

Examples:
    # one live snapshot, straight away
    python scripts/replay.py --once

    # 12h of history at 60s spacing, then a live one
    python scripts/replay.py --backfill-hours 12

    # hammer 200 samples rapidly (good for chart/ring-buffer checks)
    python scripts/replay.py --count 200 --interval 0
"""

from __future__ import annotations

import argparse
import json
import random
import time
import urllib.request

PROC_NAMES = [
    "systemd", "dockerd", "postgres", "node", "caddy", "redis-server",
    "vector", "sshd", "containerd", "cron", "postgres: writer", "python3",
]


def make_snapshot(ts: int, i: int, rng: random.Random, state: dict) -> dict:
    cores = 4
    base = 18 + 6 * rng.random()
    per_core = [round(max(1.0, min(100.0, base + rng.uniform(-14, 22))), 1) for _ in range(cores)]
    total = round(sum(per_core) / len(per_core), 1)

    mem_total = 8 * 1024**3
    mem_used = int(mem_total * (0.55 + 0.18 * rng.random()))
    mem_avail = mem_total - mem_used

    # Cumulative counters: accumulate monotonically so derived rates stay positive.
    state["net_rx"] += rng.randint(1_500_000, 9_000_000)
    state["net_tx"] += rng.randint(2_000_000, 14_000_000)
    state["dio_r"] += rng.randint(400_000, 3_000_000)
    state["dio_w"] += rng.randint(200_000, 2_000_000)
    net_rx, net_tx = state["net_rx"], state["net_tx"]
    dio_r, dio_w = state["dio_r"], state["dio_w"]

    top = [
        {
            "pid": 1024 + n,
            "name": rng.choice(PROC_NAMES),
            "cpu_pct": round(rng.uniform(0.2, 12.0), 1),
            "mem_pct": round(rng.uniform(0.5, 22.0), 1),
        }
        for n in range(8)
    ]
    top_cpu = sorted(top, key=lambda p: p["cpu_pct"], reverse=True)
    top_mem = sorted(top, key=lambda p: p["mem_pct"], reverse=True)

    # One disk scan, refreshed every 12h.
    scanned_at = int(ts // (12 * 3600) * (12 * 3600))

    return {
        "ts": ts,
        "hostname": "prod-fra-web01",
        "uptime_s": 42 * 86400 + 18 * 3600 + 32 * 60 + i * 60,
        "load": [round(base / 40, 2), round(base / 45, 2), round(base / 50, 2)],
        "cpu": {"total_pct": total, "per_core_pct": per_core},
        "mem": {
            "total": mem_total,
            "used": mem_used,
            "available": mem_avail,
            "swap_total": 2 * 1024**3,
            "swap_used": int(0.15 * 2 * 1024**3),
        },
        "disks": [
            {"mount": "/", "total": 120 * 1024**3, "used": 48 * 1024**3},
            {"mount": "/var", "total": 40 * 1024**3, "used": int((12 + i % 6) * 1024**3)},
        ],
        "net": {"rx_bytes": net_rx, "tx_bytes": net_tx},
        "diskio": {"read_bytes": dio_r, "write_bytes": dio_w},
        "disk_space": {
            "scanned_at": scanned_at,
            "roots": ["/"],
            "top_dirs": [
                {"path": "/var/lib/docker", "bytes": 21 * 1024**3},
                {"path": "/var/log", "bytes": 8 * 1024**3},
                {"path": "/usr", "bytes": 6 * 1024**3},
                {"path": "/home", "bytes": 3 * 1024**3},
            ],
            "top_files": [
                {"path": "/var/log/syslog", "bytes": 5 * 1024**3},
                {"path": "/var/lib/docker/overlay2/ab12/diff.bin", "bytes": 3 * 1024**3},
                {"path": "/var/log/journal/1a2b.journal", "bytes": 2 * 1024**3},
            ],
        },
        "processes": {"count": 84 + i % 10, "top_cpu": top_cpu, "top_mem": top_mem},
    }


def post(url: str, token: str, payload: dict) -> int:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        return res.status


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8099/ingest")
    ap.add_argument("--token", default="dev")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--interval", type=float, default=0.0, help="seconds between posts")
    ap.add_argument("--backfill-hours", type=float, default=0.0, help="seed this many hours of 60s samples")
    ap.add_argument("--once", action="store_true", help="post a single snapshot now")
    args = ap.parse_args()

    rng = random.Random(7)
    now = int(time.time())
    state: dict = {}

    # Counters must stay monotonic across backfill + live samples, otherwise the
    # derived rates read negative deltas (which the API correctly reports as a reset).
    state["net_rx"] = 400 * 1024**3
    state["net_tx"] = 1400 * 1024**3
    state["dio_r"] = 55_000_000_000
    state["dio_w"] = 44_000_000_000
    seq = 0
    if args.backfill_hours > 0:
        n = int(args.backfill_hours * 60)
        start = now - n * 60
        for i in range(n):
            post(args.url, args.token, make_snapshot(start + i * 60, i, rng, state))
        seq = n
        print(f"backfilled {n} samples over {args.backfill_hours}h")

    total = 1 if args.once else args.count
    for i in range(total):
        status = post(args.url, args.token, make_snapshot(now + i, seq + i, rng, state))
        print(f"posted ts={now + i} -> {status}")
        if args.interval and i < total - 1:
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
