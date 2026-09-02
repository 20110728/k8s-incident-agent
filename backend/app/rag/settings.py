from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RagSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    dashscope_api_key: SecretStr
    dashscope_base_url: str

    embedding_model: str = "qwen3.7-text-embedding"
    embedding_dimensions: int = Field(
        default=1024,
        ge=256,
        le=2560,
    )

    pgvector_url: str
    runbook_collection: str = "k8s_runbooks_v1"

    llm_model: str = "qwen3.7-plus"
    llm_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
    )
    llm_max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
    )


@lru_cache
def get_rag_settings() -> RagSettings:
    return RagSettings()