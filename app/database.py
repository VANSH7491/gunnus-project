from __future__ import annotations

import asyncio
import logging
from typing import Optional, Protocol

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo.errors import PyMongoError

from app.config import Settings

logger = logging.getLogger(__name__)


class DatabaseClient(Protocol):
    """Subset of the Motor client surface this app depends on."""

    def get_database(self, name: str) -> AsyncIOMotorDatabase: ...
    def close(self) -> None: ...


class MongoConnection:
    """Owns the Motor client and exposes the metadata collection.

    Connection is established lazily with retries so the API can start even
    when MongoDB is still warming up (Docker Compose start-order).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Optional[DatabaseClient] = None
        self._collection: Optional[AsyncIOMotorCollection] = None

    async def connect(self) -> None:
        retries = self._settings.mongo_connect_retries
        delay = self._settings.mongo_connect_backoff_seconds
        last_err: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                client = AsyncIOMotorClient(
                    self._settings.mongo_uri,
                    serverSelectionTimeoutMS=3000,
                    uuidRepresentation="standard",
                )
                # Force a round-trip to confirm reachability.
                await client.admin.command("ping")
                self._client = client
                db = client.get_database(self._settings.mongo_db)
                self._collection = db.get_collection(self._settings.mongo_collection)
                await self._collection.create_index("url", unique=True)
                logger.info("Connected to MongoDB at %s", self._settings.mongo_uri)
                return
            except PyMongoError as exc:
                last_err = exc
                logger.warning(
                    "MongoDB connection attempt %d/%d failed: %s", attempt, retries, exc
                )
                await asyncio.sleep(delay)
        raise RuntimeError(f"Could not connect to MongoDB after {retries} attempts: {last_err}")

    def use_client(self, client: DatabaseClient) -> None:
        """Inject a preconstructed client (used by tests)."""
        self._client = client
        db = client.get_database(self._settings.mongo_db)
        self._collection = db.get_collection(self._settings.mongo_collection)

    async def ensure_indexes(self) -> None:
        if self._collection is None:
            raise RuntimeError("Collection is not initialised")
        await self._collection.create_index("url", unique=True)

    @property
    def collection(self) -> AsyncIOMotorCollection:
        if self._collection is None:
            raise RuntimeError("Database connection is not initialised")
        return self._collection

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._collection = None
