# VPS Status Dashboard

Read-only telemetry dashboard and **ingest sink** for the
[`serverStatus`](https://github.com/papi656/serverStatus) agent.

A small agent running on a VPS POSTs one JSON snapshot per minute to `POST /ingest`.
This service validates, stores it in SQLite and renders a single-page console UI
inspired by a military instrumentation aesthetic, with **night** and **day** themes.

```
 VPS                                        this service
┌──────────────────┐   POST /ingest    ┌───────────────────────────────┐
│ serverStatus     │  Bearer token     │ FastAPI                       │
│ agent (60s loop) │ ────────────────► │  ├─ validate (Pydantic)       │
│                  │   204 No Content  │  ├─ store (SQLite, WAL)       │
└──────────────────┘                   │  └─ serve static UI + read API│
                                       └───────────────────────────────┘
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
python -m uvicorn app.main:app --host 0.0.0.0 --port 8099
```

Open <http://127.0.0.1:8099/>. With no data yet the UI shows an "awaiting
telemetry" state. Seed it with the replay tool:

```bash
python scripts/replay.py --backfill-hours 12   # 12h of 60s samples + a live one
```

Other replay modes:

```bash
python scripts/replay.py --once                # a single snapshot
python scripts/replay.py --count 200 --interval 0
```

The real agent should point at the same place:

```bash
TOKEN=dev ENDPOINT=http://127.0.0.1:8099/ingest ./server-status-agent --once
```

## Configuration

All read from the environment, optionally via a local `.env` (see `.env.example`).

| Variable | Default | Purpose |
|---|---|---|
| `INGEST_TOKEN` | `dev` | Bearer token required by `POST /ingest` |
| `HOST` / `PORT` | `0.0.0.0` / `8099` | Bind address (8099 matches the agent default) |
| `DATA_DIR` | `./data` | Directory for the SQLite database |
| `DB_PATH` | `$DATA_DIR/status.db` | Explicit database path |
| `SAMPLE_RETENTION_HOURS` | `24` | Hours of 60s samples retained |
| `SCAN_RETENTION_DAYS` | `7` | Days of 12h disk-space scans retained |
| `PRUNE_INTERVAL_SECONDS` | `300` | How often the background retention task runs |

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

## API

Everything except `/ingest` is public and read-only.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/ingest` | Bearer-authenticated. `204` on success, `401` bad token, `422` invalid payload |
| `GET` | `/api/current` | Latest sample + disks + processes + derived rates + stats + latest disk scan |
| `GET` | `/api/history?range=1h\|6h\|24h` | Downsampled series for the charts |
| `GET` | `/api/disk` | Latest disk-space scan (top dirs / files) |
| `GET` | `/api/health` | Small JSON status |
| `GET` | `/docs` | Auto-generated OpenAPI reference |

### Derived values

`net` and `diskio` arrive as **cumulative counters**. The API computes per-second
rates as `(current - previous) / (ts_current - ts_previous)`. A **negative delta
means the machine rebooted**, so the response sets `reset: true` and the UI shows a
reset instead of a bogus value.

`disk_space` only changes when `scanned_at` changes; scans are deduplicated so a
scan is stored once per 12h rather than once per 60s sample. A scan may be up to
12h stale — that is expected.

## Themes

Night is the default; a `NIGHT`/`DAY` switch in the header re-themes the entire
console via CSS custom properties and persists the choice in `localStorage`. The
initial theme follows `prefers-color-scheme`.

## Deploying

Keep the ingest route off the public internet. Two common options:

1. **Bind to loopback, reverse-proxy only the UI.** Run on `127.0.0.1:8099` and let
   Caddy/nginx expose only `/`, `/api/*`, `/styles.css`, `/app.js` publicly while
   `/ingest` is reachable by the agent over a private network or SSH tunnel.
2. **Expose `/ingest` but require the bearer token**, and rate-limit it at the proxy.

Example systemd unit:

```ini
[Unit]
Description=VPS status dashboard
After=network.target

[Service]
WorkingDirectory=/opt/status-dashboard
EnvironmentFile=/opt/status-dashboard/.env
ExecStart=/opt/status-dashboard/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8099
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```

## Notes on the payload

The agent never sends command lines or process arguments — only process `name`,
`pid` and percentages — and this UI never displays anything else. Read routes are
all read-only by design.

The full agent-side contract is documented in `AGENT-TO-DASHBOARD.md` (kept out of
git on purpose).
