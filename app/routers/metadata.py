from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.dependencies import get_repository, get_service
from app.models import AcceptedResponse, MetadataRecord, RecordStatus, URLRequest
from app.repository import MetadataRepository
from app.services import MetadataService, is_blocked_host, normalize_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/metadata", tags=["metadata"])


def _validate_and_normalize(raw_url: str, settings: Settings) -> str:
    """Normalise a user-supplied URL and apply the SSRF host policy.

    Raises HTTPException with appropriate status codes; the route handlers
    let those propagate to FastAPI's error handling.
    """
    try:
        normalized = normalize_url(raw_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    hostname = (urlsplit(normalized).hostname or "").strip()
    if is_blocked_host(hostname, allow_private=settings.allow_private_hosts):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Refusing to fetch private/loopback host {hostname!r}. "
                "Set ALLOW_PRIVATE_HOSTS=true to override (development only)."
            ),
        )
    return normalized


# ---------------------------------------------------------------------------
# POST — create / refresh a metadata record (synchronous fetch)
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=MetadataRecord,
    summary="Create or refresh a metadata record for a URL.",
    responses={
        400: {"description": "Invalid URL or blocked host."},
        502: {"description": "The target URL could not be fetched."},
    },
)
async def create_metadata(
    payload: URLRequest,
    service: MetadataService = Depends(get_service),
    repo: MetadataRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> MetadataRecord:
    """Fetch the URL synchronously, persist the result, return the record."""
    url = _validate_and_normalize(str(payload.url), settings)

    await service.collect_and_store(url)
    record = await repo.find_by_url(url)
    if record is None:  # pragma: no cover — upserts should never leave this empty
        raise HTTPException(status_code=500, detail="Record was not persisted")
    if record.status == RecordStatus.FAILED:
        raise HTTPException(status_code=502, detail=record.error or "Fetch failed")
    return record


# ---------------------------------------------------------------------------
# GET — return inventory hit or schedule async collection
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get(
    "",
    responses={
        200: {"model": MetadataRecord, "description": "Metadata found in inventory."},
        202: {
            "model": AcceptedResponse,
            "description": "Metadata not yet available; collection scheduled.",
        },
        400: {"description": "Invalid URL or blocked host."},
    },
    summary="Retrieve metadata for a URL; schedule collection on cache miss.",
)
async def get_metadata(
    url: str,
    background: BackgroundTasks,
    service: MetadataService = Depends(get_service),
    repo: MetadataRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Look up ``url`` in the inventory.

    * READY → 200 with the full record.
    * PENDING and recently updated → 202 (a collection is in flight; do
      not double-fetch).
    * Missing, FAILED, or stale PENDING → 202 and schedule a background
      task via FastAPI's in-process ``BackgroundTasks``. The collection
      runs after the response is flushed; no broker, no service-to-self
      HTTP.
    """
    normalized = _validate_and_normalize(url, settings)
    record = await repo.find_by_url(normalized)

    if record is not None:
        if record.status == RecordStatus.READY:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=record.model_dump(mode="json"),
            )
        if record.status == RecordStatus.PENDING:
            age = (_now() - record.updated_at).total_seconds()
            if age < settings.pending_grace_seconds:
                # An earlier request already scheduled a collector.
                # Don't fire a duplicate task — Mongo upserts are
                # idempotent but the upstream fetch is wasted work.
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content=AcceptedResponse(
                        url=normalized,
                        detail="Collection in progress; retry shortly.",
                    ).model_dump(mode="json"),
                )
            logger.info(
                "Pending record for %s is %.1fs old; treating as stuck and retrying",
                normalized,
                age,
            )
        # status == FAILED or stale PENDING falls through to a fresh attempt.

    await repo.mark_pending(normalized)
    background.add_task(service.collect_and_store, normalized)

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=AcceptedResponse(url=normalized).model_dump(mode="json"),
    )
