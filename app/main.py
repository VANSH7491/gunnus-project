from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Dict

import httpx
from fastapi import FastAPI

from app.config import get_settings
from app.database import MongoConnection
from app.routers import metadata as metadata_router


def _configure_logging(level: str) -> None:
    """Set the root log level without trampling existing handlers.

    Uvicorn installs its own handlers before our lifespan runs; calling
    ``logging.basicConfig`` again would be a no-op AND fail to propagate
    the level. We adjust the level explicitly so our log messages surface
    at the configured threshold regardless of who configured the root
    logger first.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    if not root.handlers:
        logging.basicConfig(
            level=level.upper(),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level)
    log = logging.getLogger(__name__)

    mongo = MongoConnection(settings)
    await mongo.connect()
    app.state.mongo = mongo

    # One long-lived httpx client per process — keeps the connection pool,
    # DNS cache, and TLS context warm across requests and background tasks.
    http_client = httpx.AsyncClient(
        timeout=settings.fetch_timeout_seconds,
        follow_redirects=True,
        max_redirects=settings.fetch_max_redirects,
        headers={"User-Agent": settings.fetch_user_agent},
    )
    app.state.http_client = http_client
    log.info("Application startup complete")

    try:
        yield
    finally:
        await http_client.aclose()
        await mongo.close()
        log.info("Application shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title="HTTP Metadata Inventory Service",
        version="1.0.0",
        description=(
            "Collects HTTP headers, cookies, and page source for given URLs "
            "and serves them from a MongoDB-backed inventory. Cache misses "
            "on GET trigger asynchronous in-process collection."
        ),
        lifespan=lifespan,
    )

    @app.get("/health", tags=["health"], summary="Liveness probe")
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    app.include_router(metadata_router.router)
    return app


app = create_app()
