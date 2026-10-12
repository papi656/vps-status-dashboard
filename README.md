# VPS Status Dashboard

Read-only telemetry dashboard and **ingest sink** for the
[`serverStatus`](https://github.com/papi656/serverStatus) agent.

A small agent running on a VPS POSTs one JSON snapshot per minute to `POST /ingest`.
This service validates, stores it in SQLite and renders a single-page console UI
inspired by a military instrumentation aesthetic, with **night** and **day** themes.

It listens on **two ports**: a public one for the UI and read API, and a separate ingest
surface that is never routed publicly — so `/ingest` is unreachable from the internet
by topology, not by a reverse-proxy rule that has to be configured correctly.

```
                                          this service (one process)
  VPS                                  ┌──────────────────────────────────┐
┌────────────────┐  POST :8099/ingest   │ ingest surface   :8099           │
│ serverStatus   │  Bearer token        │  ├─ auth (constant time)         │
│ agent (60s)    │ ───────────────────► │  ├─ rate limit                   │
└────────────────┘  204 / 401 / 429     │  ├─ validate (Pydantic)          │
        host loopback only               │  └─ store (SQLite, WAL)          │
                                        │                                  │
   browser ──► proxy ──► :8098          │ public surface   :8098           │
                                        │  ├─ GET /api/*   (read-only)     │
                                        │  └─ static dashboard UI          │
                                        └──────────────────────────────────┘
```

## Stack

- **Backend:** FastAPI + Uvicorn
- **Validation:** Pydantic models mirroring the agent contract
- **Storage:** stdlib `sqlite3` (no native build step), WAL mode
- **Frontend:** vanilla HTML/CSS/JS, no framework and no build step; charts are hand-rolled SVG

## Quick start

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt

cp .env.example .env          # set INGEST_TOKEN
python -m app.serve           # 8098 = UI + read API, 8099 = ingest
```

Open <http://127.0.0.1:8098/>. With no data yet the UI shows an "awaiting
telemetry" state. Seed it with the replay tool:

```bash
python scripts/replay.py --backfill-hours 12   # 12h of 60s samples + a live one
```

Other replay modes:

```bash
python scripts/replay.py --once                # a single snapshot
python scripts/replay.py --count 200 --interval 0
```

The real agent posts to the **ingest** port, which is not the one you open in a browser:

```bash
TOKEN=dev ENDPOINT=http://127.0.0.1:8099/ingest ./server-status-agent --once
```

> `python -m app.serve` is the entrypoint because it starts both listeners. Running
> `uvicorn app.main:app` alone gives you the read-only surface with no ingest route at all.

## Configuration

All read from the environment, optionally via a local `.env` (see `.env.example`).

| Variable | Default | Purpose |
|---|---|---|
| `INGEST_TOKEN` | `dev` | Bearer token for `POST /ingest`. **Empty refuses to start** |
| `HOST` | `0.0.0.0` | Bind address for both listeners |
| `PORT` | `8098` | **Public** surface: UI + read API. Route this one |
| `INGEST_PORT` | `8099` | **Ingest** surface. Publish to loopback; never route. Matches the agent's default ENDPOINT |
| `ENABLE_DOCS` | *off* | Set `1` to expose `/docs` and `/openapi.json` |
| `DATA_DIR` | `./data` | Directory for the SQLite database |
| `DB_PATH` | `$DATA_DIR/status.db` | Explicit database path |
| `SAMPLE_RETENTION_HOURS` | `24` | Hours of 60s samples retained |
| `SCAN_RETENTION_DAYS` | `7` | Days of 12h disk-space scans retained |
| `PRUNE_INTERVAL_SECONDS` | `300` | How often the background retention task runs |
| `INGEST_RATE_LIMIT_SECONDS` | `30` | Min seconds between accepted ingests (0 disables) |
| `MAX_INGEST_BODY_BYTES` | `1048576` | Reject ingest bodies larger than this |
| `CLOCK_SKEW_SECONDS` | `300` | Allowed agent/dashboard clock skew |

### Retention

Rows outside the window are deleted **twice over**, so the timeline holds even if
the agent stops reporting:

1. **On every ingest** — cheap, indexed deletes at the tail of the write transaction.
2. **On a background timer** (default every 5 min, plus once at boot) — so data still
   expires when no snapshots are arriving.

Child rows cascade (`disks`, `processes`, `disk_items` via `ON DELETE CASCADE`), and
each background pass runs `PRAGMA wal_checkpoint(TRUNCATE)` to fold the WAL back into
the main file. Deletions are logged, e.g.:

```
2026-10-11 18:50:48 | INFO | vpsdash | retention: pruned 2 sample(s) older than 24h
```

The database file size is **bounded**: at 60 s cadence a 24 h window is ~1440 samples
(≈1.5 MB) and SQLite reuses freed pages for new inserts, so it plateaus rather than
growing without limit. A physical `VACUUM` is not needed for that reason, but can be
run by hand if you ever delete a large backlog at once.

## Protecting the ingest route

`POST /ingest` is the only writable surface, so it is defended in layers:

| Layer | Behaviour |
|---|---|
| **Separate port** | Served on `INGEST_PORT`, which the proxy never routes — unreachable publicly by topology |
| **Bearer auth** | Constant-time token compare; `401` before anything else runs |
| **Rate limit** | At most one accepted request per `INGEST_RATE_LIMIT_SECONDS`; `429` with `Retry-After` |
| **Body cap** | `413` for bodies over `MAX_INGEST_BODY_BYTES`, checked before parsing |
| **Timestamp sanity** | `422` for a `ts` in the future, or older than the retention window |
| **Collection caps** | `422` for oversized lists (cores, mounts, processes, scan items, paths) |

The limiter is **global, not per-IP** — a single host with a single token, so one shared
budget that can't be widened by rotating source addresses. Auth runs **before** the
limiter on purpose: if an unauthenticated caller could consume the budget, anyone could
lock the real agent out with one junk request every 30s.

The timestamp check is the one that actually prevents permanent poisoning. Retention
prunes on `ts < now - window`, so a row dated years into the future would **never** be
reclaimed — a valid token could slowly fill the disk forever, one row per interval. The
collection caps stop a single request from writing millions of rows or multi-megabyte
path strings.

The limiter is in-process (a restart resets the window, which only the operator can do),
and its counters are exposed on `/api/health` and `/api/current`, then rendered in the UI
as `INGEST RATE LIMIT` and `RATE-LIMITED HITS`.

> **Seeding:** `scripts/replay.py` posts rapidly and will hit `429`. Restart the server
> with `INGEST_RATE_LIMIT_SECONDS=0`, or use `--interval 60` to respect the limit.

## Matching the `serverStatus` agent

The wire contract is `shared::Snapshot` in
[`papi656/serverStatus`](https://github.com/papi656/serverStatus) and matches these
models field-for-field.

**The agent does not retry.** Its loop logs `push failed: …` and waits for the next
interval, so any rejected snapshot is a permanent gap. Two settings must therefore stay
compatible:

| Agent setting | Must relate to | Otherwise |
|---|---|---|
| `INTERVAL_SECS` (default `60`) | `> INGEST_RATE_LIMIT_SECONDS` (default `30`) | Agent gets `429` and drops samples |
| VPS clock accuracy | `CLOCK_SKEW_SECONDS` (default `300`) | Agent stamps `SystemTime::now()`; a clock >5 min fast makes **every** snapshot `422` |

Both rejections are logged (`ingest rejected (422) at 'ts': …` and
`ingest rate limited (…)`), so a gap is visible in the container logs rather than silent.
If the box has no reliable NTP, raise `CLOCK_SKEW_SECONDS`.

**The endpoint is plain HTTP on loopback.** The agent posts to
`http://127.0.0.1:8099/ingest` — never the public HTTPS domain. The proxy terminates TLS
for browsers; the agent does not go through it. This is exactly why the two-port split
matters: the only write path is reachable over loopback, and the publicly routed port
does not serve `/ingest` at all.

### Data semantics worth knowing

- **`mem.used` is `total - available`.** A buffer/cache figure (`total - used - available`)
  is therefore always exactly `0`, which is why the RAM panel shows only used/available.
- **`net` sums all non-loopback interfaces; `diskio` sums all block devices** (skipping
  only `loop*`/`ram*`). Losing an interface or device makes the sum go *backwards*, so a
  negative delta alone is not proof of a reboot. The reset flag requires `uptime_s` to
  fall, and otherwise clamps to `0` instead of reporting a reboot that did not happen.
- **`diskio` counts whole disks *and* their partitions** (`sda` and `sda1`), so absolute
  I/O totals are inflated; the per-second rates are still directionally useful.
- **Process `cpu_pct` is `delta/total × ncpu × 100`**, so it can legitimately exceed
  `100%` on a multi-core box — the model bounds it at `>= 0` only.
- **`cpu.total_pct` cannot exceed `100`** by construction (`idle + iowait` are a subset of
  the summed fields) and the agent guards the zero-delta case, so no `NaN` reaches the wire.

## API

Two surfaces, two ports. Everything on the public port is read-only; the only write
endpoint in the service lives on the ingest port.

**Ingest surface — `:8099`**

| Method | Path | Notes |
|---|---|---|
| `POST` | `/ingest` | Bearer-authenticated. `204` ok, `401` bad token, `429` rate limited, `413` body too large, `422` invalid payload. Nothing else exists here. |

**Public surface — `:8098`**

| Method | Path | Notes |
|---|---|---|
| `GET` | `/` | The dashboard UI |
| `GET` | `/api/current` | Latest sample + disks + processes + derived rates + stats + latest disk scan |
| `GET` | `/api/history?range=1h\|6h\|24h` | Downsampled series for the charts |
| `GET` | `/api/disk` | Latest disk-space scan (top dirs / files) |
| `GET` | `/api/health` | Small JSON status |
| `GET` | `/docs` | Only when `ENABLE_DOCS=1` — off by default, it advertises the ingest contract |

### Derived values

`net` and `diskio` arrive as **cumulative counters**. The API computes per-second
rates as `(current - previous) / (ts_current - ts_previous)`. A negative delta alone is
not enough to call a reboot — the agent sums across interfaces and devices, so losing one
also goes backwards. The response sets `reset: true` only when `uptime_s` fell as well,
and otherwise clamps the rate to `0`.

`disk_space` only changes when `scanned_at` changes; scans are deduplicated so a
scan is stored once per 12h rather than once per 60s sample. A scan may be up to
12h stale — that is expected.

## Themes

Night is the default; a `NIGHT`/`DAY` switch in the header re-themes the entire
console via CSS custom properties and persists the choice in `localStorage`. The
initial theme follows `prefers-color-scheme`.

## Deploying

**Coolify / Docker:** see [`docs/DEPLOY-COOLIFY.md`](docs/DEPLOY-COOLIFY.md) for the full
walkthrough. The short version:

```bash
cp .env.example .env && echo "INGEST_TOKEN=$(openssl rand -hex 32)" >> .env
docker compose up -d --build
```

Route **8098** through the proxy; **8099** is published to host loopback for the agent
and must never be routed.

Three things will bite you if skipped:

1. **Set `INGEST_TOKEN`.** Empty refuses to start; `dev` logs a warning and is public.
2. **Persist `/data`.** Otherwise every redeploy wipes the history.
3. **Publish only loopback for the ingest port** — `127.0.0.1:8099:8099`, not `8099:8099`.

**Bare metal / systemd**, if you'd rather not use containers:

```ini
[Unit]
Description=VPS status dashboard
After=network.target

[Service]
WorkingDirectory=/opt/status-dashboard
EnvironmentFile=/opt/status-dashboard/.env
ExecStart=/opt/status-dashboard/.venv/bin/python -m app.serve
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```

Running as a plain process means both listeners share the host's network namespace, so
the isolation above depends on the proxy only forwarding `8098` — and on the ingest port
not being reachable from outside (bind `HOST=127.0.0.1` if the proxy runs on the same
host and the agent is local).

## Notes on the payload

The agent never sends command lines or process arguments — only process `name`,
`pid` and percentages — and this UI never displays anything else. Read routes are
all read-only by design.

The full agent-side contract is documented in `AGENT-TO-DASHBOARD.md` (kept out of
git on purpose).
