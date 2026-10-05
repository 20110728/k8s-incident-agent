from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


APP_SCHEMA = "incident_agent_app"


class CursorPort(Protocol):
    def execute(
        self,
        query: str,
        params: tuple[Any, ...] | None = None,
    ) -> Any:
        ...

    def fetchall(self) -> list[Any]:
        ...


class CursorContextPort(Protocol):
    def __enter__(self) -> CursorPort:
        ...

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        ...


class TransactionContextPort(Protocol):
    def __enter__(self) -> Any:
        ...

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        ...


class MigrationConnectionPort(Protocol):
    def cursor(self) -> CursorContextPort:
        ...

    def transaction(self) -> TransactionContextPort:
        ...


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        version=1,
        name="create_incidents",
        statements=(
            """
            CREATE TABLE incident_agent_app.incidents (
                incident_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL UNIQUE,
                namespace VARCHAR(63) NOT NULL,
                service_name VARCHAR(63) NOT NULL,
                description TEXT NOT NULL,
                phase TEXT NOT NULL DEFAULT 'created',
                created_at TIMESTAMPTZ NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT incidents_incident_id_not_blank
                    CHECK (btrim(incident_id) <> ''),
                CONSTRAINT incidents_thread_id_not_blank
                    CHECK (btrim(thread_id) <> ''),
                CONSTRAINT incidents_namespace_not_blank
                    CHECK (btrim(namespace) <> ''),
                CONSTRAINT incidents_service_name_not_blank
                    CHECK (btrim(service_name) <> ''),
                CONSTRAINT incidents_description_not_blank
                    CHECK (btrim(description) <> ''),
                CONSTRAINT incidents_phase_not_blank
                    CHECK (btrim(phase) <> '')
            )
            """,
            """
            CREATE INDEX incidents_created_at_idx
            ON incident_agent_app.incidents (
                created_at DESC,
                incident_id DESC
            )
            """,
        ),
    ),
    Migration(
        version=2,
        name="create_rechecks",
        statements=(
            """CREATE TABLE incident_agent_app.rechecks (
                sequence BIGSERIAL PRIMARY KEY,
                recheck_id TEXT NOT NULL UNIQUE,
                incident_id TEXT NOT NULL REFERENCES incident_agent_app.incidents(incident_id) ON DELETE CASCADE,
                result JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE INDEX rechecks_incident_sequence_idx
                ON incident_agent_app.rechecks (incident_id, sequence DESC)""",
        ),
    ),
    Migration(
        version=3,
        name="create_runs",
        statements=(
            """CREATE TABLE incident_agent_app.runs (
                run_id TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL REFERENCES incident_agent_app.incidents(incident_id),
                thread_id TEXT NOT NULL UNIQUE,
                run_kind TEXT NOT NULL DEFAULT 'diagnosis' CHECK (run_kind = 'diagnosis'),
                parent_run_id TEXT REFERENCES incident_agent_app.runs(run_id),
                input_revision INTEGER NOT NULL DEFAULT 1 CHECK (input_revision >= 1),
                input_payload JSONB NOT NULL CHECK (jsonb_typeof(input_payload) = 'object'),
                input_sha256 TEXT NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
                workflow_version TEXT NOT NULL DEFAULT 'incident-v1',
                idempotency_scope TEXT,
                idempotency_key TEXT,
                request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
                status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
                    'queued','running','waiting_user','waiting_approval',
                    'retry_scheduled','reconciling','succeeded','failed','cancelled')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMPTZ,
                last_error JSONB,
                attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
                lease_owner TEXT,
                lease_epoch BIGINT NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
                lease_expires_at TIMESTAMPTZ,
                heartbeat_at TIMESTAMPTZ,
                next_retry_at TIMESTAMPTZ,
                CONSTRAINT runs_idempotency_pair CHECK (
                    (idempotency_scope IS NULL AND idempotency_key IS NULL) OR
                    (idempotency_scope IS NOT NULL AND idempotency_key IS NOT NULL)),
                CONSTRAINT runs_idempotency_unique UNIQUE (idempotency_scope,idempotency_key)
            )""",
            """CREATE UNIQUE INDEX runs_one_active_per_incident
                ON incident_agent_app.runs (incident_id)
                WHERE status IN ('queued','running','waiting_user','waiting_approval','retry_scheduled','reconciling')""",
            """CREATE INDEX runs_incident_created_idx ON incident_agent_app.runs
                (incident_id, created_at DESC, run_id DESC)""",
        ),
    ),
)


def run_migrations(
    connection: MigrationConnectionPort,
) -> list[int]:
    """Apply pending application migrations in one transaction."""

    applied_now: list[int] = []

    with connection.transaction():
        with connection.cursor() as cursor:
            # Serialize application migrations across simultaneous API starts.
            cursor.execute("SELECT pg_advisory_xact_lock(734219801)")
            cursor.execute(
                "CREATE SCHEMA IF NOT EXISTS "
                f"{APP_SCHEMA}"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS
                    incident_agent_app.schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    )
                """
            )
            cursor.execute(
                """
                SELECT version
                FROM incident_agent_app.schema_migrations
                ORDER BY version
                """
            )
            applied_versions = {
                int(row["version"])
                for row in cursor.fetchall()
            }

            for migration in MIGRATIONS:
                if migration.version in applied_versions:
                    continue

                for statement in migration.statements:
                    cursor.execute(statement)

                cursor.execute(
                    """
                    INSERT INTO
                        incident_agent_app.schema_migrations (
                            version,
                            name
                        )
                    VALUES (%s, %s)
                    """,
                    (
                        migration.version,
                        migration.name,
                    ),
                )
                applied_now.append(migration.version)

    return applied_now
