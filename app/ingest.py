"""The ingest surface: the only writable part of the service.

Deliberately a **separate ASGI app on its own port** (see ``app.serve``). The reverse
proxy only ever routes the public port, so ``/ingest`` is unreachable from the internet
because of the network topology — not because a proxy rule was configured correctly.

Everything here is defence in depth on top of that:

1. ``require_auth``      — constant-time bearer compare, evaluated before the body.
2. ``enforce_rate_limit`` — one accepted request per ``INGEST_RATE_LIMIT_SECONDS``.
3. ``limit_ingest_body`` — refuse oversized bodies before parsing.
4. Pydantic              — field and collection bounds, plus timestamp sanity.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db
from .config import settings
from .models import Snapshot
from .ratelimit import MinIntervalLimiter

log = logging.getLogger("vpsdash.ingest")

limiter = MinIntervalLimiter(settings.ingest_rate_limit_seconds)
_last_limit_log = 0.0
_last_auth_log = 0.0

# Counters so a failed ingest is diagnosable from the public API alone, without a
# shell on the box: "samples: 0" cannot otherwise distinguish an agent that never
# connected from one whose token was wrong.
_counters_lock = threading.Lock()
_counters = {"accepted": 0, "auth_failed": 0, "validation_failed": 0}


def _bump(key: str) -> None:
    with _counters_lock:
        _counters[key] += 1

# No docs/openapi here: this app should not describe itself to anyone who finds it.
ingest_app = FastAPI(
    title="VPS Status Dashboard — ingest",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """Route-level dependency, so auth is checked *before* the body is validated."""
    global _last_auth_log
    expected = f"Bearer {settings.ingest_token}"
    if hmac.compare_digest(authorization or "", expected):
        return

    _bump("auth_failed")
    # Throttled: auth runs before the rate limiter, so unauthenticated attempts are
    # unbounded and logging each one would itself be a way to fill the disk.
    now = time.monotonic()
    if now - _last_auth_log >= 60:
        _last_auth_log = now
        log.warning(
            "ingest rejected (401): bad bearer token (%d total)",
            _counters["auth_failed"],
        )
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
    global _last_limit_log
    wait = limiter.check()
    if wait <= 0:
        return

    # Throttle the log: a flood would otherwise fill the log with one line per hit.
    now = time.monotonic()
    if now - _last_limit_log >= 60:
        _last_limit_log = now
        log.warning(
            "ingest rate limited (%d rejected so far) — sender is faster than %ds",
            limiter.rejected,
            settings.ingest_rate_limit_seconds,
        )

    raise HTTPException(
        status_code=429,
        detail=(
            "ingest rate limited: at most one request per "
            f"{settings.ingest_rate_limit_seconds}s"
        ),
        headers={"Retry-After": str(int(wait) + 1)},
    )


@ingest_app.middleware("http")
async def limit_ingest_body(request: Request, call_next):
    """Refuse oversized bodies before FastAPI parses them."""
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


@ingest_app.exception_handler(RequestValidationError)
async def log_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Log *why* a snapshot was rejected.

    The agent has no retry (see its main loop), so a 422 here is a permanent gap in
    the timeline. Without this the rejection is completely silent.
    """
    _bump("validation_failed")
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body") or "body"
    log.warning("ingest rejected (422) at '%s': %s", loc, first.get("msg", "invalid payload"))
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


@ingest_app.post(
    "/ingest",
    status_code=204,
    dependencies=[Depends(require_auth), Depends(enforce_rate_limit)],
)
def ingest(snapshot: Snapshot) -> Response:
    """Receive one snapshot from the agent. Bearer-authenticated, write-only."""
    db.store_snapshot(snapshot)
    _bump("accepted")
    return Response(status_code=204)


def ingest_guard() -> dict[str, object]:
    """Public view of the ingest protections, surfaced in the UI."""
    with _counters_lock:
        snapshot = dict(_counters)
    return {
        "rate_limit_s": settings.ingest_rate_limit_seconds,
        "rejected": limiter.rejected,  # 429s (rate limit)
        "accepted": snapshot["accepted"],
        "auth_failed": snapshot["auth_failed"],
        "validation_failed": snapshot["validation_failed"],
        "max_body_bytes": settings.max_body_bytes,
        "clock_skew_s": settings.clock_skew_seconds,
        "ingest_port": settings.ingest_port,
    }
