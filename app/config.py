from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables.

    Field aliases (UPPER_SNAKE) match the names exposed via Docker Compose
    and ``.env`` so operators don't need to know the Python attribute
    names. ``populate_by_name=True`` keeps either form working.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- MongoDB ---------------------------------------------------------
    mongo_uri: str = Field(default="mongodb://localhost:27017", alias="MONGO_URI")
    mongo_db: str = Field(default="metadata_inventory", alias="MONGO_DB")
    mongo_collection: str = Field(default="pages", alias="MONGO_COLLECTION")
    # Retry on startup so the API survives Mongo booting after it.
    mongo_connect_retries: int = Field(default=10, alias="MONGO_CONNECT_RETRIES")
    mongo_connect_backoff_seconds: float = Field(
        default=1.5, alias="MONGO_CONNECT_BACKOFF_SECONDS"
    )

    # --- Outbound fetcher ------------------------------------------------
    fetch_timeout_seconds: float = Field(default=15.0, alias="FETCH_TIMEOUT_SECONDS")
    fetch_max_bytes: int = Field(default=5_000_000, alias="FETCH_MAX_BYTES")
    fetch_max_redirects: int = Field(default=5, alias="FETCH_MAX_REDIRECTS")
    # Honest, neutral UA — many sites reject blank/`python-httpx` UAs.
    fetch_user_agent: str = Field(
        default="Mozilla/5.0 (compatible; metadata-inventory/1.0)",
        alias="FETCH_USER_AGENT",
    )

    # --- SSRF defence-in-depth ------------------------------------------
    # Reject IP-literal URLs aimed at loopback / private / link-local hosts
    # by default. Set ALLOW_PRIVATE_HOSTS=true for local development where
    # you want to fetch from `http://localhost:...` etc.
    allow_private_hosts: bool = Field(default=False, alias="ALLOW_PRIVATE_HOSTS")

    # --- Concurrency control --------------------------------------------
    # A "pending" record younger than this many seconds is assumed to have
    # an in-flight background task; a duplicate GET will NOT re-fire the
    # collector. Records older than this are treated as stuck and retried.
    pending_grace_seconds: float = Field(default=30.0, alias="PENDING_GRACE_SECONDS")

    # --- Logging ---------------------------------------------------------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
