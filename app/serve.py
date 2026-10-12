"""Entrypoint: serve the public surface and the ingest surface on separate ports.

Isolation is a property of the topology, not of a reverse-proxy rule. The ingest port is
published to host loopback only and is never routed by the proxy, so ``/ingest`` cannot
be reached from the internet however the proxy is configured. A misconfigured proxy
exposes the read-only UI, not a writable endpoint.

Run with ``python -m app.serve``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import uvicorn

from .config import settings
from .ingest import ingest_app
from .main import app as public_app

log = logging.getLogger("vpsdash")


def _preflight() -> None:
    """Fail fast on configurations that would defeat the split or the auth."""
    if settings.ingest_port == settings.port:
        raise SystemExit(
            "INGEST_PORT and PORT must differ — with one port the ingest surface "
            "would be publicly routable."
        )
    if not settings.ingest_token.strip():
        raise SystemExit(
            "INGEST_TOKEN is empty — refusing to start, since an empty token is "
            "effectively no authentication. Set it to a strong secret."
        )
    if settings.ingest_token == "dev":
        log.warning(
            "INGEST_TOKEN is still the default 'dev'. This value is in a public "
            "repository — set a strong secret before exposing this host."
        )


async def _serve() -> None:
    public = uvicorn.Server(
        uvicorn.Config(public_app, host=settings.host, port=settings.port, log_level="info")
    )
    ingest = uvicorn.Server(
        uvicorn.Config(ingest_app, host=settings.host, port=settings.ingest_port, log_level="info")
    )

    log.info("public surface on %s:%d  — route this one", settings.host, settings.port)
    log.info(
        "ingest surface on %s:%d/ingest  — host loopback only, never route this",
        settings.host,
        settings.ingest_port,
    )

    tasks = [
        asyncio.create_task(public.serve(), name="public"),
        asyncio.create_task(ingest.serve(), name="ingest"),
    ]
    # Both servers install the signal handler and each only flags itself, so a SIGTERM
    # stops whichever caught it; the wait below then takes the other one down too. If
    # either listener dies on its own, the whole process exits so the supervisor
    # (Docker / Coolify) restarts it rather than leaving a half-dead container.
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    public.should_exit = True
    ingest.should_exit = True
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    _preflight()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve())


if __name__ == "__main__":
    main()
