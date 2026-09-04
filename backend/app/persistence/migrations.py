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
)


def run_migrations(
    connection: MigrationConnectionPort,
) -> list[int]:
    """Apply pending application migrations in one transaction."""

    applied_now: list[int] = []

    with connection.transaction():
        with connection.cursor() as cursor:
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