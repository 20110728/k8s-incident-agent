from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_prefix="INCIDENT_AGENT_WORKER_")
    heartbeat_seconds: float = Field(default=5, ge=0.1, le=60)
    lease_seconds: float = Field(default=30, ge=1, le=300)
    poll_seconds: float = Field(default=1, ge=0.1, le=30)
    max_attempts: int = Field(default=3, ge=1, le=10)
    shutdown_seconds: float = Field(default=20, ge=1, le=120)

    @model_validator(mode="after")
    def heartbeat_fits_lease(self):
        if self.heartbeat_seconds * 3 > self.lease_seconds:
            raise ValueError("lease must be at least three heartbeat intervals")
        return self
