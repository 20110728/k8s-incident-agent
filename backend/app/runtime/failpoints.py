"""Opt-in, one-shot barriers; never enabled against an application database."""
from functools import lru_cache
from pathlib import Path
import threading
import time
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.app.runtime.telemetry import report

POINTS = frozenset({"after_accept", "after_claim", "after_evidence_checkpoint", "model_call",
    "before_operation_read", "before_operation_prepare", "after_operation_prepare",
    "before_patch", "after_patch_response", "before_operation_record", "after_operation_result"})


class FailureSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_prefix="INCIDENT_AGENT_TEST_", populate_by_name=True)
    failpoint: str | None = None
    failpoint_mode: Literal["pause", "storage_error"] = "pause"
    barrier_dir: Path | None = None
    environment: str = Field(default="production", validation_alias="INCIDENT_AGENT_API_ENVIRONMENT")


class Failpoints:
    def __init__(self, settings, *, database_name=None):
        self.settings, self.fired, self.lock = settings, set(), threading.Lock()
        if settings.failpoint:
            if (settings.environment != "test" or settings.failpoint not in POINTS or
                    not database_name or not database_name.startswith("incident_agent_test_") or
                    settings.barrier_dir is None):
                raise ValueError("failpoints require test environment, isolated test database and barrier directory")

    def hit(self, point, lease=None, operation_id=None):
        if self.settings.failpoint != point:
            return
        with self.lock:
            if point in self.fired:
                return
            self.fired.add(point)
        directory = self.settings.barrier_dir
        directory.mkdir(parents=True, exist_ok=True)
        report("failpoint_reached", lease, node=point, operation_id=operation_id)
        (directory / (point + ".reached")).write_text("reached", encoding="utf-8")
        if self.settings.failpoint_mode == "storage_error":
            from psycopg import OperationalError
            report("storage_failure_injected", lease, node=point, error_class="transient")
            raise OperationalError("injected test storage failure")
        deadline = time.monotonic() + 60
        while not (directory / (point + ".release")).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("test barrier timed out")
            time.sleep(0.05)


@lru_cache
def get_failpoints():
    settings = FailureSettings()
    name = None
    if settings.failpoint:
        if settings.environment != "test":
            raise ValueError("failpoints require test environment and isolated test database")
        from psycopg.conninfo import conninfo_to_dict
        from backend.app.persistence.database import normalize_psycopg_dsn
        from backend.app.persistence.settings import get_database_settings
        name = conninfo_to_dict(normalize_psycopg_dsn(get_database_settings().database_url.get_secret_value())).get("dbname")
    return Failpoints(settings, database_name=name)


def hit(point, lease=None, operation_id=None):
    get_failpoints().hit(point, lease, operation_id)
