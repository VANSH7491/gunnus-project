"""Hermetic tests for the metadata inventory service.

These exercise the FastAPI app via its ASGI transport with both Mongo and
the outbound HTTP client stubbed out. They are fast (<1s total) and
deterministic, so they run by default with ``pytest``.

For real-network coverage see ``tests/test_integration.py``.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services import is_blocked_host, normalize_url


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://HttpBin.org/", "https://httpbin.org/"),
        ("HTTP://HttpBin.org:80/get?q=1", "http://httpbin.org/get?q=1"),
        ("https://httpbin.org/get#frag", "https://httpbin.org/get"),
        ("https://httpbin.org", "https://httpbin.org/"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_normalize_url_rejects_missing_scheme():
    with pytest.raises(ValueError):
        normalize_url("httpbin.org")


@pytest.mark.parametrize(
    "host, allow_private, blocked",
    [
        ("httpbin.org", False, False),
        ("github.com", False, False),
        ("localhost", False, True),
        ("127.0.0.1", False, True),
        ("10.0.0.5", False, True),
        ("192.168.1.10", False, True),
        ("169.254.169.254", False, True),  # AWS metadata service — classic SSRF target
        ("0.0.0.0", False, True),
        ("::1", False, True),
        # Override flag bypasses the guard:
        ("127.0.0.1", True, False),
        ("localhost", True, False),
    ],
)
def test_is_blocked_host(host, allow_private, blocked):
    assert is_blocked_host(host, allow_private=allow_private) is blocked


# ---------------------------------------------------------------------------
# API surface tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health(app_and_client):
    _, client = app_and_client
    res = await client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_post_creates_record(app_and_client, transport_handler):
    _, client = app_and_client
    transport_handler["https://httpbin.org/get"] = lambda req: httpx.Response(
        200,
        headers={"content-type": "application/json", "set-cookie": "sid=abc; Path=/"},
        text='{"args": {}}',
    )

    res = await client.post("/metadata", json={"url": "https://httpbin.org/get"})
    assert res.status_code == 201
    body = res.json()
    assert body["url"] == "https://httpbin.org/get"
    assert body["final_url"] == "https://httpbin.org/get"
    assert body["status"] == "ready"
    assert body["status_code"] == 200
    assert body["page_source"] == '{"args": {}}'
    assert body["cookies"] == {"sid": "abc"}
    assert body["set_cookie_headers"] == ["sid=abc; Path=/"]
    assert body["headers"]["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_post_preserves_multiple_set_cookie_headers(app_and_client, transport_handler):
    """Multi-cookie responses must not lose data — dict(headers) collapses them."""
    _, client = app_and_client
    transport_handler["https://httpbin.org/cookies/set"] = lambda req: httpx.Response(
        200,
        headers=[
            ("content-type", "text/html"),
            ("set-cookie", "a=1; Path=/"),
            ("set-cookie", "b=2; Path=/; HttpOnly"),
        ],
        text="<html/>",
    )

    res = await client.post("/metadata", json={"url": "https://httpbin.org/cookies/set"})
    assert res.status_code == 201
    body = res.json()
    assert body["set_cookie_headers"] == ["a=1; Path=/", "b=2; Path=/; HttpOnly"]
    assert body["cookies"] == {"a": "1", "b": "2"}


@pytest.mark.asyncio
async def test_post_normalizes_url(app_and_client):
    _, client = app_and_client
    res = await client.post("/metadata", json={"url": "HTTPS://HttpBin.org"})
    assert res.status_code == 201
    assert res.json()["url"] == "https://httpbin.org/"


@pytest.mark.asyncio
async def test_post_returns_502_on_fetch_failure(app_and_client, transport_handler):
    _, client = app_and_client

    def boom(req):
        raise httpx.ConnectError("upstream unreachable")

    transport_handler["https://broken.test/"] = boom

    res = await client.post("/metadata", json={"url": "https://broken.test/"})
    assert res.status_code == 502
    assert "upstream unreachable" in res.json()["detail"]


@pytest.mark.asyncio
async def test_get_cache_miss_returns_202_and_schedules_collection(
    app_and_client, transport_handler
):
    app, client = app_and_client
    transport_handler["https://httpbin.org/uuid"] = lambda req: httpx.Response(
        200,
        headers={"x-fresh": "1", "content-type": "application/json"},
        text='{"uuid":"deadbeef"}',
    )

    res = await client.get("/metadata", params={"url": "https://httpbin.org/uuid"})
    assert res.status_code == 202
    body = res.json()
    assert body["status"] == "pending"
    assert body["url"] == "https://httpbin.org/uuid"

    # The background task should complete shortly after the response; poll
    # briefly to absorb scheduling jitter under the ASGI transport.
    record = None
    for _ in range(20):
        record = await app.state.mongo.collection.find_one({"url": "https://httpbin.org/uuid"})
        if record and record.get("status") == "ready":
            break
        await asyncio.sleep(0.05)

    assert record is not None
    assert record["status"] == "ready"
    assert record["page_source"] == '{"uuid":"deadbeef"}'
    assert record["headers"].get("x-fresh") == "1"
    assert record["final_url"] == "https://httpbin.org/uuid"


@pytest.mark.asyncio
async def test_get_cache_hit_returns_record(app_and_client, transport_handler):
    _, client = app_and_client
    transport_handler["https://news.ycombinator.com/"] = lambda req: httpx.Response(
        200, text="<html>cached</html>"
    )

    post = await client.post("/metadata", json={"url": "https://news.ycombinator.com/"})
    assert post.status_code == 201

    res = await client.get("/metadata", params={"url": "https://news.ycombinator.com/"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ready"
    assert body["page_source"] == "<html>cached</html>"


@pytest.mark.asyncio
async def test_get_dedupes_concurrent_cache_misses(app_and_client, transport_handler):
    """A fresh GET for a URL whose record is already PENDING must not re-fire the collector."""
    app, client = app_and_client
    call_count = {"n": 0}

    def slow_handler(req):
        call_count["n"] += 1
        return httpx.Response(200, text="<html>slow</html>")

    transport_handler["https://httpbin.org/delay/0"] = slow_handler

    # Seed a PENDING record directly so we deterministically hit the
    # "already in flight" branch without relying on async timing.
    await app.state.mongo.collection.insert_one(
        {
            "url": "https://httpbin.org/delay/0",
            "status": "pending",
            "headers": {},
            "set_cookie_headers": [],
            "cookies": {},
            "page_source": None,
            "status_code": None,
            "final_url": None,
            "error": None,
            "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        }
    )

    res = await client.get("/metadata", params={"url": "https://httpbin.org/delay/0"})
    assert res.status_code == 202
    assert "in progress" in res.json()["detail"]
    assert call_count["n"] == 0  # no upstream fetch was issued


@pytest.mark.asyncio
async def test_get_retries_stale_pending(app_and_client, transport_handler, settings):
    """A PENDING record older than the grace window is treated as stuck and retried."""
    from datetime import datetime, timedelta, timezone

    app, client = app_and_client
    transport_handler["https://httpbin.org/anything"] = lambda req: httpx.Response(
        200, text="<html>recovered</html>"
    )

    stale = datetime.now(timezone.utc) - timedelta(seconds=settings.pending_grace_seconds + 5)
    await app.state.mongo.collection.insert_one(
        {
            "url": "https://httpbin.org/anything",
            "status": "pending",
            "headers": {},
            "set_cookie_headers": [],
            "cookies": {},
            "page_source": None,
            "status_code": None,
            "final_url": None,
            "error": None,
            "created_at": stale,
            "updated_at": stale,
        }
    )

    res = await client.get("/metadata", params={"url": "https://httpbin.org/anything"})
    assert res.status_code == 202

    record = None
    for _ in range(20):
        record = await app.state.mongo.collection.find_one({"url": "https://httpbin.org/anything"})
        if record and record.get("status") == "ready":
            break
        await asyncio.sleep(0.05)
    assert record is not None
    assert record["status"] == "ready"
    assert record["page_source"] == "<html>recovered</html>"


@pytest.mark.asyncio
async def test_get_rejects_invalid_url(app_and_client):
    _, client = app_and_client
    res = await client.get("/metadata", params={"url": "not-a-url"})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_post_rejects_invalid_url(app_and_client):
    _, client = app_and_client
    res = await client.post("/metadata", json={"url": "not-a-url"})
    assert res.status_code == 422  # Pydantic HttpUrl rejects upstream of the route


@pytest.mark.asyncio
async def test_post_rejects_private_host_by_default(app_and_client, settings):
    """SSRF guard must reject loopback URLs unless explicitly enabled."""
    # The shared fixture has ALLOW_PRIVATE_HOSTS=True; override to False
    # for this one test by writing settings.allow_private_hosts directly.
    settings.allow_private_hosts = False
    _, client = app_and_client

    res = await client.post("/metadata", json={"url": "http://127.0.0.1/secret"})
    assert res.status_code == 400
    assert "private/loopback" in res.json()["detail"]
