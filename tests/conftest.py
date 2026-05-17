"""Shared pytest fixtures.

The hermetic test suite drives the FastAPI app through ``httpx.ASGITransport``
with two stubs in place of real infrastructure:

* ``mongomock-motor`` replaces MongoDB so tests do not need a running
  database container.
* ``httpx.MockTransport`` replaces the outbound HTTP client so tests do
  not touch the network.

The end-to-end live tests live in ``tests/test_integration.py`` behind
the ``integration`` marker and DO hit the real network — they are opt-in
(``pytest -m integration``).
"""
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

# A small bit of real-looking HTML used as the default mocked response.
DEFAULT_BODY = "<html><head><title>Test page</title></head><body>Hello</body></html>"


@pytest.fixture
def settings() -> Settings:
    """Settings for the hermetic test stack.

    ``ALLOW_PRIVATE_HOSTS=True`` is required because the mocked URLs do
    not resolve via DNS — we don't want the SSRF guard rejecting them
    before MockTransport gets a chance to serve them.
    """
    return Settings(
        MONGO_URI="mongodb://test/",
        MONGO_DB="metadata_inventory_test",
        MONGO_COLLECTION="pages",
        FETCH_TIMEOUT_SECONDS=5.0,
        ALLOW_PRIVATE_HOSTS=True,
        PENDING_GRACE_SECONDS=30.0,
        LOG_LEVEL="WARNING",
    )


@pytest.fixture
def transport_handler() -> Dict[str, Callable[[httpx.Request], httpx.Response]]:
    """Mutable URL→handler registry consulted by the mocked transport."""
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

    The real lifespan would dial a live MongoDB; we bypass it and populate
    ``app.state`` directly with the same objects it would create.
    """
    app = create_app()

    mongo = MongoConnection(settings)
    mongo.use_client(AsyncMongoMockClient())
    await mongo.ensure_indexes()
    app.state.mongo = mongo

    mocked_http = httpx.AsyncClient(transport=mock_transport)
    app.state.http_client = mocked_http

    # The cached get_settings() must yield OUR settings, not whatever the
    # environment provides.
    app.dependency_overrides[get_settings] = lambda: settings

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield app, client

    await mocked_http.aclose()
    await mongo.close()
