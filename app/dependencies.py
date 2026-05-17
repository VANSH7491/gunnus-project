from __future__ import annotations

from typing import Optional

import httpx
from fastapi import Depends, Request

from app.config import Settings, get_settings
from app.database import MongoConnection
from app.repository import MetadataRepository
from app.services import MetadataFetcher, MetadataService


def get_mongo(request: Request) -> MongoConnection:
    mongo: Optional[MongoConnection] = getattr(request.app.state, "mongo", None)
    if mongo is None:
        raise RuntimeError("Mongo connection is not configured on app.state")
    return mongo


def get_http_client(request: Request) -> httpx.AsyncClient:
    client: Optional[httpx.AsyncClient] = getattr(request.app.state, "http_client", None)
    if client is None:
        raise RuntimeError("HTTP client is not configured on app.state")
    return client


def get_repository(mongo: MongoConnection = Depends(get_mongo)) -> MetadataRepository:
    return MetadataRepository(mongo.collection)


def get_fetcher(
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> MetadataFetcher:
    return MetadataFetcher(settings, client=client)


def get_service(
    repo: MetadataRepository = Depends(get_repository),
    fetcher: MetadataFetcher = Depends(get_fetcher),
) -> MetadataService:
    return MetadataService(repo, fetcher)
