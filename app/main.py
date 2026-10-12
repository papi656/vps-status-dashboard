"""FastAPI application: authenticated ingest sink + public read-only API + static UI."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import settings
from .models import Snapshot
from .ratelimit import MinIntervalLimiter

log = logging.getLogger("vpsdash")

# Uvicorn only configures its own loggers; give app-level logs a handler too.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

limiter = MinIntervalLimiter(settings.ingest_rate_limit_seconds)


async def _retention_loop() -> None:
    """Enforce retention on a timer so it does not depend on incoming samples."""
    while True:
        await asyncio.sleep(settings.prune_interval_seconds)
        try:
            deleted = await asyncio.to_thread(db.prune_now)
            if deleted:
                log.info("retention: pruned %d sample(s) older than %dh", deleted, settings.sample_retention_hours)
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
    description="Ingest sink and read-only telemetry dashboard for the serverStatus agent.",
    lifespan=lifespan,
)


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """Route-level dependency, so auth is checked *before* the body is validated."""
    expected = f"Bearer {settings.ingest_token}"
    if not hmac.compare_digest(authorization or "", expected):
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


def enforce_rate_limit() -> None:
    """Global ingest gate. Runs after auth, before the body is parsed.

    Auth first, deliberately: if an unauthenticated caller could trip the limiter,
    anyone could lock the real agent out with a junk request every 30s.
    """
    wait = limiter.check()
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail=(
                "ingest rate limited: at most one request per "
                f"{settings.ingest_rate_limit_seconds}s"
            ),
            headers={"Retry-After": str(int(wait) + 1)},
        )


@app.middleware("http")
async def limit_ingest_body(request, call_next):
    """Refuse oversized ingest bodies before FastAPI parses them."""
    if request.url.path == "/ingest":
        raw = request.headers.get("content-length")
        if raw is not None:
            try:
                length = int(raw)
            except ValueError:
                length = 0
            if length > settings.max_body_bytes:
                return JSONResponse(
                    {"detail": f"payload too large (max {settings.max_body_bytes} bytes)"},
                    status_code=413,
                )
    return await call_next(request)


# --------------------------------------------------------------------- ingest (private)


@app.post(
    "/ingest",
    status_code=204,
    dependencies=[Depends(require_auth), Depends(enforce_rate_limit)],
)
def ingest(snapshot: Snapshot) -> Response:
    """Receive one snapshot from the agent. Bearer-authenticated, write-only."""
    db.store_snapshot(snapshot)
    return Response(status_code=204)


# ---------------------------------------------------------------------- read (public)

RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600}


@app.get("/api/current")
def api_current() -> JSONResponse:
    data = db.get_current()
    data["ingest"] = _ingest_guard()
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
        "ingest": _ingest_guard(),
    }


def _ingest_guard() -> dict[str, object]:
    """Public view of the ingest protections, surfaced in the UI."""
    return {
        "rate_limit_s": settings.ingest_rate_limit_seconds,
        "rejected": limiter.rejected,
        "max_body_bytes": settings.max_body_bytes,
        "clock_skew_s": settings.clock_skew_seconds,
    }


# ------------------------------------------------------------------------------ static

app.mount("/", StaticFiles(directory=str(settings.public_dir), html=True), name="ui")
