# Deploying on Coolify

Two listeners in one container:

| Port | Surface | Exposure |
|---|---|---|
| `8099` | dashboard UI + read-only `/api/*` | **route this** through the proxy |
| `8098` | `POST /ingest` only | publish to host loopback; **never route** |

The agent runs on the same host and posts to `http://127.0.0.1:8098/ingest`. Because the
proxy only ever reaches `8099`, `/ingest` is unreachable from the internet — a
misconfigured proxy can expose the read-only UI but not a writable endpoint.

---

## 1. Create the resource

**New Resource → your repository**, and pick one:

- **Docker Compose** — use the `docker-compose.yml` in this repo unchanged. It already
  declares the volume, the loopback port publish, and the healthcheck.
- **Dockerfile** — Coolify needs two settings that the Dockerfile cannot express:
  - **Ports Exposes** = `8099` (this is what Traefik routes)
  - **Ports Mappings** = `127.0.0.1:8098:8098` (host loopback → ingest)

## 2. Environment variables

Set these in **Environment Variables**:

```bash
INGEST_TOKEN=$(openssl rand -hex 32)   # REQUIRED — app refuses to start if empty
```

The rest have sane defaults and only need overriding if you change behaviour:

```bash
INGEST_RATE_LIMIT_SECONDS=30   # must stay well below the agent's INTERVAL_SECS (60)
MAX_INGEST_BODY_BYTES=1048576
CLOCK_SKEW_SECONDS=300
SAMPLE_RETENTION_HOURS=24
SCAN_RETENTION_DAYS=7
PRUNE_INTERVAL_SECONDS=300
# ENABLE_DOCS=1                # off by default: /docs advertises the ingest contract
```

Changing any of these needs a redeploy to take effect.

> The container logs a `WARNING` on startup if `INGEST_TOKEN` is still `dev`, and
> **refuses to start** if it is empty.

## 3. Persistent storage

Attach storage to **`/data`**. Without it, every redeploy recreates the SQLite database
and you lose the whole 24 h window.

The container runs as **uid `10001`**. A Docker-managed volume inherits the right
ownership automatically. If you use a **bind mount** instead, the host directory must be
writable by that uid or the app will fail to open the database:

```bash
sudo mkdir -p /opt/vpsdash-data && sudo chown 10001:10001 /opt/vpsdash-data
```

## 4. Domain

Assign your domain to the service and make sure it targets **`8099`**. Do not add a
second domain or route for the ingest port.

## 5. Verify from outside

```bash
D=https://your-domain

curl -s $D/api/health                                   # {"status":"ok",...}

# These must NOT be 200/204/401 — 404/405/403 all mean correct:
curl -s -o /dev/null -w '%{http_code}\n' -X POST $D/ingest   # 405 (static mount) — good
curl -s -o /dev/null -w '%{http_code}\n' $D/docs             # 404 — good
```

If `POST $D/ingest` ever returns `401`, that is a **failure** — it means the ingest
surface is reachable publicly. Check that the proxy routes only `8099`.

## 6. Deploy the agent (on the same host)

From [`papi656/serverStatus`](https://github.com/papi656/serverStatus):

```bash
cargo build --release -p server-status-agent
sudo install -m 0755 target/release/server-status-agent /usr/local/bin/
```

`/etc/server-status-agent.env`:

```bash
ENDPOINT=http://127.0.0.1:8098/ingest
TOKEN=<the same value as INGEST_TOKEN>
INTERVAL_SECS=60
```

```bash
sudo systemctl enable --now server-status-agent
journalctl -u server-status-agent -f     # expect: pushed ts=...
```

### Clock

The agent stamps `SystemTime::now()`. If the VPS clock drifts more than
`CLOCK_SKEW_SECONDS` (default 300) ahead of the container, **every** snapshot is rejected
with `422` and the dashboard silently stops updating. Confirm NTP is running:

```bash
timedatectl status        # "System clock synchronized: yes"
```

## 7. Confirm end-to-end

```bash
curl -s https://your-domain/api/health
#   samples climbing, last_age_s < 90
```

In the dashboard, the **RECEIVER / INGEST STATUS** panel should show `LAST SAMPLE` a few
seconds old and `RATE-LIMITED HITS` at `0`. Then restart the container — history should
survive.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| App exits at startup: `INGEST_TOKEN is empty` | Set the env var |
| Agent logs `401` | Token mismatch between `INGEST_TOKEN` and `TOKEN` |
| Agent logs `429` | `INTERVAL_SECS` too close to `INGEST_RATE_LIMIT_SECONDS` |
| Agent logs `422 ... in the future` | VPS clock ahead of the container; fix NTP or raise `CLOCK_SKEW_SECONDS` |
| Agent logs `422` about a list/collection | Payload exceeded a bound; see the message |
| Agent logs connection refused | Port mapping missing, or the agent points at `8099` instead of `8098` |
| History empty after every redeploy | `/data` storage not attached |
| `unable to open database file` | Bind mount not owned by uid `10001` |
| Dashboard says `TELEMETRY STALE` | Agent not running, or being rejected — check its journal |
| `POST https://domain/ingest` returns `401` | The proxy is routing the ingest port — fix the route |
