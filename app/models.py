from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, HttpUrl


class RecordStatus(str, Enum):
    """Lifecycle state of a stored metadata record."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class URLRequest(BaseModel):
    """Inbound payload carrying a target URL."""

    url: HttpUrl


class MetadataRecord(BaseModel):
    """Canonical representation of a stored metadata document.

    ``headers`` is a single-value-per-name map (lossy for repeated headers,
    but the common case). ``set_cookie_headers`` preserves the raw
    ``Set-Cookie`` lines verbatim — a response can legally set several
    cookies in separate headers and we don't want to drop any of them.
    ``cookies`` is the parsed ``name -> value`` view for convenient lookup.
    """

    url: str
    status: RecordStatus
    status_code: Optional[int] = None
    headers: Dict[str, str] = Field(default_factory=dict)
    set_cookie_headers: List[str] = Field(default_factory=list)
    cookies: Dict[str, str] = Field(default_factory=dict)
    page_source: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_document(cls, doc: Dict[str, Any]) -> "MetadataRecord":
        payload = {k: v for k, v in doc.items() if k != "_id"}
        return cls.model_validate(payload)


class AcceptedResponse(BaseModel):
    """Returned when a fetch has been scheduled but no data is available yet."""

    url: str
    status: RecordStatus = RecordStatus.PENDING
    detail: str = "Metadata collection scheduled. Retry shortly."
