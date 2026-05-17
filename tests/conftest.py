from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Dict

import httpx
import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient

from app.config import Settings, get_settings
from app.database import MongoConnection
from app.main import create_app

DEFAULT_BODY = "<html><head><title>Example</title></head><body>Hello</body></html>"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        MONGO_URI="mongodb://test/",
        MONGO_DB="metadata_inventory_test",
        MONGO_COLLECTION="pages",
        FETCH_TIMEOUT_SECONDS=5.0,
        LOG_LEVEL="WARNING",
    )


@pytest.fixture
def transport_handler() -> Dict[str, Callable[[httpx.Request], httpx.Response]]:
    """Mutable registry of per-URL handlers used by the mocked transport."""
    return {}


@pytest.fixture
def mock_transport(transport_handler):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in transport_handler:
            return transport_handler[url](request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8", "x-test": "yes"},
            text=DEFAULT_BODY,
        )

    return httpx.MockTransport(handler)


@pytest_asyncio.fixture
async def app_and_client(settings, mock_transport) -> AsyncIterator[tuple]:
    """FastAPI app wired against an in-memory Mongo and a mocked httpx client.

    The real lifespan would dial a live MongoDB, so we bypass it and
    populate ``app.state`` directly with the same objects it would create
    (mocked Mongo + httpx client backed by ``MockTransport``).
    """
    app = create_app()

    mongo = MongoConnection(settings)
    mongo.use_client(AsyncMongoMockClient())
    await mongo.ensure_indexes()
    app.state.mongo = mongo

    # Match the lifespan: app.state.http_client is the shared client. The
    # mock transport plugs in instead of a real network stack.
    mocked_http = httpx.AsyncClient(transport=mock_transport)
    app.state.http_client = mocked_http

    # Ensure the cached get_settings() doesn't leak a real one from the env.
    app.dependency_overrides[get_settings] = lambda: settings

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield app, client

    await mocked_http.aclose()
    await mongo.close()
