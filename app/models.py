from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


class RecordStatus(str, Enum):
    """Lifecycle state of a stored metadata record.

    pending → an active or queued collection attempt
    ready   → headers/cookies/page source are present
    failed  → last attempt errored; details live in ``error``
    """

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class URLRequest(BaseModel):
    """POST body: the URL to inventory.

    ``HttpUrl`` validates scheme + host; SSRF and host-policy rules are
    enforced separately at the route layer so they can be configured.
    """

    url: HttpUrl


class MetadataRecord(BaseModel):
    """Canonical persisted shape — also returned by the API.

    ``headers`` is a single-value-per-name map (the common case). The one
    response header that legitimately repeats is ``Set-Cookie``, captured
    verbatim in ``set_cookie_headers``. ``cookies`` is the parsed
    ``name -> value`` view for ergonomic queries.

    ``final_url`` is the URL that actually served the response after
    redirects — useful when the inventory key (the URL the user asked
    about) differs from the resource that ultimately replied.
    """

    url: str
    final_url: Optional[str] = None
    status: RecordStatus
    status_code: Optional[int] = None
    headers: Dict[str, str] = Field(default_factory=dict)
    set_cookie_headers: List[str] = Field(default_factory=list)
    cookies: Dict[str, str] = Field(default_factory=dict)
    page_source: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def _force_utc(cls, value: datetime) -> datetime:
        # BSON datetimes are stored as naive UTC. Attach the tzinfo on
        # read so callers can safely compare against ``datetime.now(UTC)``
        # without TypeError surprises.
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @classmethod
    def from_document(cls, doc: Dict[str, Any]) -> "MetadataRecord":
        # MongoDB injects "_id" automatically; strip it so the Pydantic
        # model stays a pure domain object.
        payload = {k: v for k, v in doc.items() if k != "_id"}
        return cls.model_validate(payload)


class AcceptedResponse(BaseModel):
    """Returned with HTTP 202 — collection has been scheduled."""

    url: str
    status: RecordStatus = RecordStatus.PENDING
    detail: str = "Metadata collection scheduled. Retry shortly."
