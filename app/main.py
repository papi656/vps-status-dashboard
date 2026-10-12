"""The public surface: read-only API plus the static dashboard.

The write path is **not here** — it lives in ``app.ingest`` and is served on a separate
port by ``app.serve``, so a request to ``/ingest`` on this surface is simply a 404.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import settings
from .ingest import ingest_guard

log = logging.getLogger("vpsdash")

# Uvicorn only configures its own loggers; give app-level logs a handler too.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )


async def _retention_loop() -> None:
    """Enforce retention on a timer so it does not depend on incoming samples."""
    while True:
        await asyncio.sleep(settings.prune_interval_seconds)
        try:
            deleted = await asyncio.to_thread(db.prune_now)
            if deleted:
                log.info(
                    "retention: pruned %d sample(s) older than %dh",
                    deleted,
                    settings.sample_retention_hours,
                )
        except Exception:  # never let the loop die on a transient DB error
            log.exception("retention pass failed")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    # Prune once at boot, then on the configured interval.
    with contextlib.suppress(Exception):
        await asyncio.to_thread(db.prune_now)
    task = asyncio.create_task(_retention_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="VPS Status Dashboard",
    version="1.0.0",
    description="Read-only telemetry dashboard for the serverStatus agent.",
    lifespan=lifespan,
    # Off unless ENABLE_DOCS=1: the schema advertises the ingest contract.
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600}


@app.get("/api/current")
def api_current() -> JSONResponse:
    data = db.get_current()
    data["ingest"] = ingest_guard()
    return JSONResponse(data)


@app.get("/api/history")
def api_history(range: str = Query(default="24h", pattern="^(1h|6h|24h)$")) -> JSONResponse:
    return JSONResponse(db.get_history(RANGES[range]))


@app.get("/api/disk")
def api_disk() -> JSONResponse:
    scan = db.get_disk()
    if scan is None:
        raise HTTPException(status_code=404, detail="no disk scan yet")
    return JSONResponse(scan)


@app.get("/api/health")
def api_health() -> dict[str, object]:
    stats = db.get_stats()
    last_ts = stats["last_ts"]
    return {
        "status": "ok",
        "time": int(time.time()),
        "samples": stats["samples"],
        "last_ts": last_ts,
        "last_age_s": (int(time.time()) - last_ts) if last_ts else None,
        # Exposed so persistence can be checked without a shell on the box: if samples
        # reset after a redeploy while db_path stayed the same, the mount is not durable.
        "db_path": str(settings.db_path),
        "ingest": ingest_guard(),
    }


# ------------------------------------------------------------------------------ static

app.mount("/", StaticFiles(directory=str(settings.public_dir), html=True), name="ui")
