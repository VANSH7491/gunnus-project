import asyncio

import httpx
import pytest

from app.services import normalize_url


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://Example.com/", "https://example.com/"),
        ("HTTP://Example.com:80/path?q=1", "http://example.com/path?q=1"),
        ("https://example.com/path#frag", "https://example.com/path"),
        ("https://example.com", "https://example.com/"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_normalize_url_rejects_missing_scheme():
    with pytest.raises(ValueError):
        normalize_url("example.com")


@pytest.mark.asyncio
async def test_health(app_and_client):
    _, client = app_and_client
    res = await client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_post_creates_record(app_and_client, transport_handler):
    _, client = app_and_client
    transport_handler["https://example.com/"] = lambda req: httpx.Response(
        200,
        headers={"content-type": "text/html", "set-cookie": "sid=abc; Path=/"},
        text="<html>hi</html>",
    )

    res = await client.post("/metadata", json={"url": "https://example.com/"})
    assert res.status_code == 201
    body = res.json()
    assert body["url"] == "https://example.com/"
    assert body["status"] == "ready"
    assert body["status_code"] == 200
    assert body["page_source"] == "<html>hi</html>"
    assert body["cookies"] == {"sid": "abc"}
    assert body["set_cookie_headers"] == ["sid=abc; Path=/"]
    assert "content-type" in body["headers"]


@pytest.mark.asyncio
async def test_post_preserves_multiple_set_cookie_headers(app_and_client, transport_handler):
    """Multi-cookie responses must not lose data — dict(headers) collapses them."""
    _, client = app_and_client
    transport_handler["https://multi.test/"] = lambda req: httpx.Response(
        200,
        headers=[
            ("content-type", "text/html"),
            ("set-cookie", "a=1; Path=/"),
            ("set-cookie", "b=2; Path=/; HttpOnly"),
        ],
        text="<html/>",
    )

    res = await client.post("/metadata", json={"url": "https://multi.test/"})
    assert res.status_code == 201
    body = res.json()
    assert body["set_cookie_headers"] == ["a=1; Path=/", "b=2; Path=/; HttpOnly"]
    assert body["cookies"] == {"a": "1", "b": "2"}


@pytest.mark.asyncio
async def test_post_normalizes_url(app_and_client):
    _, client = app_and_client
    res = await client.post("/metadata", json={"url": "HTTPS://Example.com"})
    assert res.status_code == 201
    assert res.json()["url"] == "https://example.com/"


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
    transport_handler["https://fresh.test/"] = lambda req: httpx.Response(
        200, headers={"x-fresh": "1"}, text="<html>fresh</html>"
    )

    res = await client.get("/metadata", params={"url": "https://fresh.test/"})
    assert res.status_code == 202
    body = res.json()
    assert body["status"] == "pending"
    assert body["url"] == "https://fresh.test/"

    # Background task should have completed by the time the response returned
    # in the ASGI test transport. Poll briefly to absorb any scheduling jitter.
    record = None
    for _ in range(20):
        record = await app.state.mongo.collection.find_one({"url": "https://fresh.test/"})
        if record and record.get("status") == "ready":
            break
        await asyncio.sleep(0.05)

    assert record is not None
    assert record["status"] == "ready"
    assert record["page_source"] == "<html>fresh</html>"
    assert record["headers"].get("x-fresh") == "1"


@pytest.mark.asyncio
async def test_get_cache_hit_returns_record(app_and_client, transport_handler):
    _, client = app_and_client
    transport_handler["https://cached.test/"] = lambda req: httpx.Response(
        200, text="<html>cached</html>"
    )

    post = await client.post("/metadata", json={"url": "https://cached.test/"})
    assert post.status_code == 201

    res = await client.get("/metadata", params={"url": "https://cached.test/"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ready"
    assert body["page_source"] == "<html>cached</html>"


@pytest.mark.asyncio
async def test_get_rejects_invalid_url(app_and_client):
    _, client = app_and_client
    res = await client.get("/metadata", params={"url": "not-a-url"})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_post_rejects_invalid_url(app_and_client):
    _, client = app_and_client
    res = await client.post("/metadata", json={"url": "not-a-url"})
    assert res.status_code == 422  # Pydantic HttpUrl validation
