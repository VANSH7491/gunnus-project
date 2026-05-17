from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables.

    Field aliases (uppercase) match the names exposed via Docker Compose /
    .env so operators don't have to remember the Python attribute names.
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
    mongo_connect_retries: int = Field(default=10, alias="MONGO_CONNECT_RETRIES")
    mongo_connect_backoff_seconds: float = Field(
        default=1.5, alias="MONGO_CONNECT_BACKOFF_SECONDS"
    )

    # --- Outbound fetcher ------------------------------------------------
    fetch_timeout_seconds: float = Field(default=15.0, alias="FETCH_TIMEOUT_SECONDS")
    fetch_max_bytes: int = Field(default=5_000_000, alias="FETCH_MAX_BYTES")
    fetch_max_redirects: int = Field(default=5, alias="FETCH_MAX_REDIRECTS")
    fetch_user_agent: str = Field(
        default="MetadataInventoryBot/1.0 (+https://example.com/bot)",
        alias="FETCH_USER_AGENT",
    )

    # --- Logging ---------------------------------------------------------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
