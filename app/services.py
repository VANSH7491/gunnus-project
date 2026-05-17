from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import Settings
from app.repository import MetadataRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL normalisation & SSRF defence
# ---------------------------------------------------------------------------


def normalize_url(raw: str) -> str:
    """Return the canonical key form used as the inventory primary key.

    Rules:
        - scheme + host are lowercased
        - default ports (:80 for http, :443 for https) are stripped
        - URL fragments (#...) are dropped — they never reach the server
        - empty path is normalised to "/"
        - query string is preserved (?a=1 and ?a=2 are distinct pages)
    """
    parts = urlsplit(raw.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"URL is missing scheme or host: {raw!r}")

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    netloc = host

    # Re-attach a non-default port if present
    if parts.port and not (
        (scheme == "http" and parts.port == 80)
        or (scheme == "https" and parts.port == 443)
    ):
        netloc = f"{netloc}:{parts.port}"

    # Re-attach userinfo if any (rare but supported by the URL grammar)
    if parts.username:
        auth = parts.username
        if parts.password:
            auth = f"{auth}:{parts.password}"
        netloc = f"{auth}@{netloc}"

    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def is_blocked_host(hostname: str, *, allow_private: bool) -> bool:
    """Defence-in-depth check against trivial SSRF.

    Catches IP-literal URLs aimed at loopback / private / link-local /
    reserved / multicast ranges plus the well-known ``localhost`` /
    ``0.0.0.0`` aliases. It does NOT perform DNS resolution: a hostname
    that resolves to a private address (DNS rebinding, internal CNAME)
    will pass this check — defeat that with egress firewalling at the
    network layer.

    Set ``allow_private=True`` (env ``ALLOW_PRIVATE_HOSTS=true``) to
    disable the check, e.g. when fetching from ``localhost`` during
    local development.
    """
    if allow_private:
        return False
    if not hostname:
        return True
    if hostname.lower() in {"localhost", "0.0.0.0"}:
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        # Not an IP literal — DNS-name path, let it through.
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchResult:
    """Outcome of a single upstream fetch."""

    status_code: int
    final_url: str
    headers: Dict[str, str]
    set_cookie_headers: List[str]
    cookies: Dict[str, str]
    body: str


class MetadataFetcher:
    """Fetches a URL and returns headers, cookies, and page source.

    The httpx client is injected so its lifecycle (connection pool, DNS
    cache, TLS context) is owned by the app, not by per-request scopes.
    For ad-hoc use (tests, scripts) the fetcher will create a short-lived
    client if none is provided.
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
            # Cap the stored body to a configured size. We measure bytes
            # so we don't blow Mongo's 16MB doc limit, but truncate by
            # characters once we know the cap was exceeded — close enough
            # for the html-heavy traffic we expect.
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
                final_url=str(response.url),
                headers=dict(response.headers),
                # Multi-value Set-Cookie must be preserved lossless: a
                # single response can legally set several cookies in
                # separate headers, and dict(headers) collapses them.
                set_cookie_headers=response.headers.get_list("set-cookie"),
                cookies=dict(response.cookies),
                body=body,
            )
        finally:
            if owns_client:
                await client.aclose()


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MetadataService:
    """Orchestrates fetch + persistence. Failures become FAILED records."""

    def __init__(self, repo: MetadataRepository, fetcher: MetadataFetcher) -> None:
        self._repo = repo
        self._fetcher = fetcher

    async def collect_and_store(self, url: str) -> None:
        """Fetch the URL and persist the result. Errors are recorded, not raised.

        This is the single callable invoked both by POST (synchronously,
        in the request handler) and by GET-on-miss (asynchronously, via
        FastAPI's BackgroundTasks). Keeping it free of HTTP concerns
        means the background-task path stays a plain in-process await —
        no broker, no service-to-self loopback.
        """
        try:
            result = await self._fetcher.fetch(url)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Fetch failed for %s: %s", url, exc)
            await self._repo.mark_failed(url, error=f"{type(exc).__name__}: {exc}")
            return

        await self._repo.upsert_ready(
            url,
            status_code=result.status_code,
            final_url=result.final_url,
            headers=result.headers,
            set_cookie_headers=result.set_cookie_headers,
            cookies=result.cookies,
            page_source=result.body,
        )
        logger.info(
            "Stored metadata for %s (final=%s status=%s bytes=%d)",
            url,
            result.final_url,
            result.status_code,
            len(result.body),
        )
