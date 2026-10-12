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
import os

import uvicorn

from .config import ROOT, settings
from .ingest import ingest_app
from .main import app as public_app

log = logging.getLogger("vpsdash")


def _preflight() -> None:
    """Fail fast on configurations that would defeat the split or the auth."""
    if settings.public_port == settings.ingest_port:
        raise SystemExit(
            "PUBLIC_PORT and INGEST_PORT must differ — with one port the ingest "
            "surface would be publicly routable."
        )
    if settings.ingest_token.strip() == "":
        raise SystemExit(
            "INGEST_TOKEN is empty — refusing to start, since an empty token is "
            "effectively no authentication. Set it to a strong secret."
        )
    if settings.ingest_token == "dev":
        log.warning(
            "INGEST_TOKEN is still the default 'dev'. This value is in a public "
            "repository — set a strong secret before exposing this host."
        )
    # A platform-injected PORT used to silently relocate the public surface. Say so
    # rather than let it look like the app ignored the setting.
    platform_port = os.environ.get("PORT")
    if platform_port and platform_port != str(settings.public_port):
        log.warning(
            "Ignoring platform PORT=%s — this app listens on PUBLIC_PORT=%d "
            "(UI/API) and INGEST_PORT=%d (/ingest). Set the proxy to route %d.",
            platform_port,
            settings.public_port,
            settings.ingest_port,
            settings.public_port,
        )

    # Storage: the one misconfiguration that silently loses history is a DATA_DIR on the
    # container's writable layer. It looks healthy at runtime and only shows up as an
    # empty database after the next redeploy, so name it at boot.
    log.info("storage: DATA_DIR=%s  DB=%s", settings.data_dir, settings.db_path)
    if os.path.exists("/.dockerenv") and (
        settings.data_dir == ROOT or ROOT in settings.data_dir.parents
    ):
        log.warning(
            "DATA_DIR=%s resolves inside the image, not on a mount — the writable layer "
            "is discarded on every redeploy, so history cannot survive. Attach persistent "
            "storage and point DATA_DIR at it (e.g. /data).",
            settings.data_dir,
        )


async def _serve() -> None:
    public = uvicorn.Server(
        uvicorn.Config(public_app, host=settings.host, port=settings.public_port, log_level="info")
    )
    ingest = uvicorn.Server(
        uvicorn.Config(ingest_app, host=settings.host, port=settings.ingest_port, log_level="info")
    )

    log.info("public surface on %s:%d  — route this one", settings.host, settings.public_port)
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
