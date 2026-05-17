from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import Settings
from app.repository import MetadataRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchResult:
    """Outcome of a single upstream fetch."""

    status_code: int
    headers: Dict[str, str]
    set_cookie_headers: List[str]
    cookies: Dict[str, str]
    body: str


def normalize_url(raw: str) -> str:
    """Canonical form used as the document key.

    Lowercases scheme/host, strips default ports and fragments, and ensures
    the root path is "/" so trivial variants do not produce duplicate
    entries. Query string is preserved because distinct queries describe
    distinct pages.
    """
    parts = urlsplit(raw.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"URL is missing scheme or host: {raw!r}")

    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    netloc = host.lower()
    if parts.port and not (
        (scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)
    ):
        netloc = f"{netloc}:{parts.port}"
    if parts.username:
        auth = parts.username
        if parts.password:
            auth = f"{auth}:{parts.password}"
        netloc = f"{auth}@{netloc}"

    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


class MetadataFetcher:
    """Fetches a URL and returns headers, cookies, and page source.

    The httpx client is injected so its lifecycle (connection pool, DNS
    cache, TLS context) is owned by the app, not by per-request scopes. As
    a convenience for ad-hoc use (tests, scripts) the fetcher will create
    its own short-lived client if none is provided.
    """

    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None) -> None:
        self._settings = settings
        self._client = client

    async def fetch(self, url: str) -> FetchResult:
        client = self._client or httpx.AsyncClient(
            timeout=self._settings.fetch_timeout_seconds,
            follow_redirects=True,
            max_redirects=self._settings.fetch_max_redirects,
            headers={"User-Agent": self._settings.fetch_user_agent},
        )
        owns_client = self._client is None
        try:
            response = await client.get(url)
            body = response.text
            encoded_len = len(body.encode("utf-8", errors="ignore"))
            if encoded_len > self._settings.fetch_max_bytes:
                body = body[: self._settings.fetch_max_bytes]
                logger.info(
                    "Truncated body for %s (%d -> %d bytes)",
                    url,
                    encoded_len,
                    self._settings.fetch_max_bytes,
                )
            return FetchResult(
                status_code=response.status_code,
                headers=dict(response.headers),
                set_cookie_headers=response.headers.get_list("set-cookie"),
                cookies=dict(response.cookies),
                body=body,
            )
        finally:
            if owns_client:
                await client.aclose()


class MetadataService:
    """Orchestrates fetch + persistence. Failures become FAILED records."""

    def __init__(self, repo: MetadataRepository, fetcher: MetadataFetcher) -> None:
        self._repo = repo
        self._fetcher = fetcher

    async def collect_and_store(self, url: str) -> None:
        """Fetch the URL and persist the result. Errors are recorded, not raised."""
        try:
            result = await self._fetcher.fetch(url)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Fetch failed for %s: %s", url, exc)
            await self._repo.mark_failed(url, error=str(exc))
            return

        await self._repo.upsert_ready(
            url,
            status_code=result.status_code,
            headers=result.headers,
            set_cookie_headers=result.set_cookie_headers,
            cookies=result.cookies,
            page_source=result.body,
        )
        logger.info("Stored metadata for %s (status=%s)", url, result.status_code)
