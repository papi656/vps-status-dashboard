"""FastAPI application: authenticated ingest sink + public read-only API + static UI."""

from __future__ import annotations

import hmac
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import settings
from .models import Snapshot

app = FastAPI(
    title="VPS Status Dashboard",
    version="1.0.0",
    description="Ingest sink and read-only telemetry dashboard for the serverStatus agent.",
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


# --------------------------------------------------------------------- ingest (private)


@app.post("/ingest", status_code=204, dependencies=[Depends(require_auth)])
def ingest(snapshot: Snapshot) -> Response:
    """Receive one snapshot from the agent. Bearer-authenticated, write-only."""
    db.store_snapshot(snapshot)
    return Response(status_code=204)


# ---------------------------------------------------------------------- read (public)

RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600}


@app.get("/api/current")
def api_current() -> JSONResponse:
    return JSONResponse(db.get_current())


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
    }


# ------------------------------------------------------------------------------ static

app.mount("/", StaticFiles(directory=str(settings.public_dir), html=True), name="ui")
