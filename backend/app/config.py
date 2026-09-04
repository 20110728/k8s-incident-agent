from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)


class ApiSettings(BaseSettings):
    """Non-secret settings for the FastAPI process."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="INCIDENT_AGENT_API_",
        extra="ignore",
    )

    app_name: str = "Kubernetes Incident Agent API"
    app_version: str = "0.1.0"
    environment: Literal[
        "development",
        "test",
        "production",
    ] = "development"
    api_prefix: str = Field(
        default="/api/v1",
        pattern=r"^/[a-zA-Z0-9/_-]*$",
    )
    docs_enabled: bool = True


@lru_cache
def get_api_settings() -> ApiSettings:
    return ApiSettings()