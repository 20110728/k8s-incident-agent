from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
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

    cors_allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )

    @field_validator("cors_allowed_origins")
    @classmethod
    def validate_cors_allowed_origins(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized_origins: list[str] = []

        for raw_origin in values:
            origin = raw_origin.strip().rstrip("/")

            if origin == "*":
                raise ValueError(
                    "wildcard CORS origins are not allowed"
                )

            parsed = urlsplit(origin)

            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    f"invalid CORS origin: {raw_origin}"
                )

            if origin not in normalized_origins:
                normalized_origins.append(origin)

        return tuple(normalized_origins)


@lru_cache
def get_api_settings() -> ApiSettings:
    return ApiSettings()