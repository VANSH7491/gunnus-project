from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from app.dependencies import get_repository, get_service
from app.models import AcceptedResponse, MetadataRecord, RecordStatus, URLRequest
from app.repository import MetadataRepository
from app.services import MetadataService, normalize_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/metadata", tags=["metadata"])


def _normalize(url: str) -> str:
    try:
        return normalize_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=MetadataRecord,
    summary="Create or refresh a metadata record for a URL.",
    responses={
        502: {"description": "The target URL could not be fetched."},
    },
)
async def create_metadata(
    payload: URLRequest,
    service: MetadataService = Depends(get_service),
    repo: MetadataRepository = Depends(get_repository),
) -> MetadataRecord:
    url = _normalize(str(payload.url))
    await service.collect_and_store(url)
    record = await repo.find_by_url(url)
    if record is None:
        raise HTTPException(status_code=500, detail="Record was not persisted")
    if record.status == RecordStatus.FAILED:
        raise HTTPException(status_code=502, detail=record.error or "Fetch failed")
    return record


@router.get(
    "",
    responses={
        200: {"model": MetadataRecord, "description": "Metadata found in inventory."},
        202: {
            "model": AcceptedResponse,
            "description": "Metadata not yet available; collection scheduled.",
        },
        400: {"description": "Invalid URL."},
    },
    summary="Retrieve metadata for a URL; schedule collection on cache miss.",
)
async def get_metadata(
    url: str,
    background: BackgroundTasks,
    service: MetadataService = Depends(get_service),
    repo: MetadataRepository = Depends(get_repository),
) -> JSONResponse:
    normalized = _normalize(url)

    record = await repo.find_by_url(normalized)
    if record is not None and record.status == RecordStatus.READY:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=record.model_dump(mode="json"),
        )

    # Cache miss (or prior failure/pending): reserve the slot and dispatch
    # collection to FastAPI's background-task runner so the response is not
    # delayed. This stays in-process — no service-to-self HTTP calls.
    await repo.mark_pending(normalized)
    background.add_task(service.collect_and_store, normalized)

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=AcceptedResponse(url=normalized).model_dump(mode="json"),
    )
