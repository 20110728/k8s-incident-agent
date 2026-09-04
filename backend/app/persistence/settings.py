from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Settings shared by application persistence components."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: SecretStr = Field(
        validation_alias="PGVECTOR_URL",
    )
    connect_timeout_seconds: int = Field(
        default=5,
        ge=1,
        le=30,
        validation_alias=(
            "INCIDENT_AGENT_DB_CONNECT_TIMEOUT_SECONDS"
        ),
    )


@lru_cache
def get_database_settings() -> DatabaseSettings:
    return DatabaseSettings()