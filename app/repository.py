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
    the application talks to this in terms of URLs and Pydantic records.
    """

    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._collection = collection

    async def find_by_url(self, url: str) -> Optional[MetadataRecord]:
        doc = await self._collection.find_one({"url": url})
        if doc is None:
            return None
        return MetadataRecord.from_document(doc)

    async def mark_pending(self, url: str) -> None:
        """Claim the URL for a collection attempt.

        Sets ``status = pending`` and refreshes ``updated_at`` on every
        call (so the route layer can use ``updated_at`` to decide if a
        prior pending claim has gone stale). Defaults for headers /
        cookies / body fields are only seeded on first insert so that
        retrying a FAILED record keeps the prior error context until the
        new attempt completes.

        Callers MUST NOT invoke this on a record that is already READY:
        the route layer guards against that.
        """
        now = _utcnow()
        await self._collection.update_one(
            {"url": url},
            {
                "$set": {
                    "status": RecordStatus.PENDING.value,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "url": url,
                    "created_at": now,
                    "final_url": None,
                    "headers": {},
                    "set_cookie_headers": [],
                    "cookies": {},
                    "page_source": None,
                    "status_code": None,
                    "error": None,
                },
            },
            upsert=True,
        )

    async def upsert_ready(
        self,
        url: str,
        *,
        status_code: int,
        final_url: str,
        headers: Dict[str, str],
        set_cookie_headers: List[str],
        cookies: Dict[str, str],
        page_source: str,
    ) -> None:
        """Persist a successful collection result for ``url``."""
        now = _utcnow()
        await self._collection.update_one(
            {"url": url},
            {
                "$set": {
                    "status": RecordStatus.READY.value,
                    "status_code": status_code,
                    "final_url": final_url,
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
        """Persist a failed collection attempt with the error message."""
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
        """Iterate every stored document. Test / admin use only."""
        return [doc async for doc in self._collection.find({})]
