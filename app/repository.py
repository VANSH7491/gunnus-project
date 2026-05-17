from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorCollection

from app.models import MetadataRecord, RecordStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MetadataRepository:
    """Data access layer over the metadata collection.

    Keeps Mongo concerns out of the routers and service layer; the rest of
    the app talks to this in terms of URLs and Pydantic records.
    """

    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._collection = collection

    async def find_by_url(self, url: str) -> Optional[MetadataRecord]:
        doc = await self._collection.find_one({"url": url})
        if doc is None:
            return None
        return MetadataRecord.from_document(doc)

    async def mark_pending(self, url: str) -> bool:
        """Reserve a pending slot. Returns True if a new doc was created."""
        now = _utcnow()
        result = await self._collection.update_one(
            {"url": url},
            {
                "$setOnInsert": {
                    "url": url,
                    "status": RecordStatus.PENDING.value,
                    "headers": {},
                    "set_cookie_headers": [],
                    "cookies": {},
                    "page_source": None,
                    "status_code": None,
                    "error": None,
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
        return result.upserted_id is not None

    async def upsert_ready(
        self,
        url: str,
        *,
        status_code: int,
        headers: Dict[str, str],
        set_cookie_headers: List[str],
        cookies: Dict[str, str],
        page_source: str,
    ) -> None:
        now = _utcnow()
        await self._collection.update_one(
            {"url": url},
            {
                "$set": {
                    "status": RecordStatus.READY.value,
                    "status_code": status_code,
                    "headers": headers,
                    "set_cookie_headers": set_cookie_headers,
                    "cookies": cookies,
                    "page_source": page_source,
                    "error": None,
                    "updated_at": now,
                },
                "$setOnInsert": {"url": url, "created_at": now},
            },
            upsert=True,
        )

    async def mark_failed(self, url: str, *, error: str) -> None:
        now = _utcnow()
        await self._collection.update_one(
            {"url": url},
            {
                "$set": {
                    "status": RecordStatus.FAILED.value,
                    "error": error,
                    "updated_at": now,
                },
                "$setOnInsert": {"url": url, "created_at": now},
            },
            upsert=True,
        )

    async def all(self) -> List[Dict[str, Any]]:
        return [doc async for doc in self._collection.find({})]
