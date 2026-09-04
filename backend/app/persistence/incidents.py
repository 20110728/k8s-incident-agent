from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import RLock
from typing import Any, Protocol

import psycopg
from psycopg.errors import UniqueViolation


class QueryResultPort(Protocol):
    def fetchone(self) -> Mapping[str, Any] | None:
        ...


class RepositoryConnectionPort(Protocol):
    def execute(
        self,
        query: str,
        params: tuple[Any, ...] | None = None,
    ) -> QueryResultPort:
        ...


ConnectionFactory = Callable[
    [],
    AbstractContextManager[RepositoryConnectionPort],
]


class IncidentRepositoryError(RuntimeError):
    """Base class for incident metadata persistence errors."""


class IncidentAlreadyExistsError(IncidentRepositoryError):
    """Raised when an incident or thread identifier already exists."""


@dataclass(frozen=True)
class NewIncidentRecord:
    incident_id: str
    thread_id: str
    namespace: str
    service_name: str
    description: str
    phase: str = "created"


@dataclass(frozen=True)
class IncidentRecord:
    incident_id: str
    thread_id: str
    namespace: str
    service_name: str
    description: str
    phase: str
    created_at: datetime
    updated_at: datetime

class IncidentRepositoryPort(Protocol):
    def create(
        self,
        record: NewIncidentRecord,
    ) -> IncidentRecord:
        ...

    def get(
        self,
        incident_id: str,
    ) -> IncidentRecord | None:
        ...

    def update_phase(
        self,
        incident_id: str,
        phase: str,
    ) -> IncidentRecord | None:
        ...

    def delete(self, incident_id: str) -> bool:
        ...


class InMemoryIncidentRepository:
    """Small isolated repository used by unit-level callers."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (
            lambda: datetime.now(UTC)
        )
        self._records: dict[str, IncidentRecord] = {}
        self._lock = RLock()

    def create(
        self,
        record: NewIncidentRecord,
    ) -> IncidentRecord:
        now = self._clock()
        stored = IncidentRecord(
            incident_id=record.incident_id,
            thread_id=record.thread_id,
            namespace=record.namespace,
            service_name=record.service_name,
            description=record.description,
            phase=record.phase,
            created_at=now,
            updated_at=now,
        )

        with self._lock:
            if record.incident_id in self._records:
                raise IncidentAlreadyExistsError(
                    "incident ID already exists"
                )

            if any(
                existing.thread_id == record.thread_id
                for existing in self._records.values()
            ):
                raise IncidentAlreadyExistsError(
                    "thread ID already exists"
                )

            self._records[record.incident_id] = stored

        return stored

    def get(
        self,
        incident_id: str,
    ) -> IncidentRecord | None:
        with self._lock:
            return self._records.get(incident_id)

    def update_phase(
        self,
        incident_id: str,
        phase: str,
    ) -> IncidentRecord | None:
        with self._lock:
            record = self._records.get(incident_id)

            if record is None:
                return None

            updated = replace(
                record,
                phase=phase,
                updated_at=self._clock(),
            )
            self._records[incident_id] = updated
            return updated

    def delete(self, incident_id: str) -> bool:
        with self._lock:
            return self._records.pop(
                incident_id,
                None,
            ) is not None

def _require_non_blank(
    value: str,
    *,
    field_name: str,
) -> str:
    normalized = value.strip()

    if not normalized:
        raise ValueError(
            f"{field_name} must not be blank"
        )

    return normalized


def _record_from_row(
    row: Mapping[str, Any],
) -> IncidentRecord:
    return IncidentRecord(
        incident_id=str(row["incident_id"]),
        thread_id=str(row["thread_id"]),
        namespace=str(row["namespace"]),
        service_name=str(row["service_name"]),
        description=str(row["description"]),
        phase=str(row["phase"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PostgresIncidentRepository:
    """Stores incident identity and request metadata in PostgreSQL."""

    def __init__(
        self,
        connection_factory: ConnectionFactory,
    ) -> None:
        self._connection_factory = connection_factory

    def create(
        self,
        record: NewIncidentRecord,
    ) -> IncidentRecord:
        values = (
            _require_non_blank(
                record.incident_id,
                field_name="incident_id",
            ),
            _require_non_blank(
                record.thread_id,
                field_name="thread_id",
            ),
            _require_non_blank(
                record.namespace,
                field_name="namespace",
            ),
            _require_non_blank(
                record.service_name,
                field_name="service_name",
            ),
            _require_non_blank(
                record.description,
                field_name="description",
            ),
            _require_non_blank(
                record.phase,
                field_name="phase",
            ),
        )

        try:
            with self._connection_factory() as connection:
                result = connection.execute(
                    """
                    INSERT INTO incident_agent_app.incidents (
                        incident_id,
                        thread_id,
                        namespace,
                        service_name,
                        description,
                        phase
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING
                        incident_id,
                        thread_id,
                        namespace,
                        service_name,
                        description,
                        phase,
                        created_at,
                        updated_at
                    """,
                    values,
                )
                row = result.fetchone()

        except UniqueViolation as error:
            raise IncidentAlreadyExistsError(
                "incident or thread ID already exists"
            ) from error
        except psycopg.Error as error:
            raise IncidentRepositoryError(
                "failed to create incident metadata"
            ) from error

        if row is None:
            raise IncidentRepositoryError(
                "create incident returned no row"
            )

        return _record_from_row(row)

    def get(
        self,
        incident_id: str,
    ) -> IncidentRecord | None:
        normalized_id = _require_non_blank(
            incident_id,
            field_name="incident_id",
        )

        try:
            with self._connection_factory() as connection:
                result = connection.execute(
                    """
                    SELECT
                        incident_id,
                        thread_id,
                        namespace,
                        service_name,
                        description,
                        phase,
                        created_at,
                        updated_at
                    FROM incident_agent_app.incidents
                    WHERE incident_id = %s
                    """,
                    (normalized_id,),
                )
                row = result.fetchone()

        except psycopg.Error as error:
            raise IncidentRepositoryError(
                "failed to read incident metadata"
            ) from error

        if row is None:
            return None

        return _record_from_row(row)

    def update_phase(
        self,
        incident_id: str,
        phase: str,
    ) -> IncidentRecord | None:
        normalized_id = _require_non_blank(
            incident_id,
            field_name="incident_id",
        )
        normalized_phase = _require_non_blank(
            phase,
            field_name="phase",
        )

        try:
            with self._connection_factory() as connection:
                result = connection.execute(
                    """
                    UPDATE incident_agent_app.incidents
                    SET
                        phase = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE incident_id = %s
                    RETURNING
                        incident_id,
                        thread_id,
                        namespace,
                        service_name,
                        description,
                        phase,
                        created_at,
                        updated_at
                    """,
                    (
                        normalized_phase,
                        normalized_id,
                    ),
                )
                row = result.fetchone()

        except psycopg.Error as error:
            raise IncidentRepositoryError(
                "failed to update incident phase"
            ) from error

        if row is None:
            return None

        return _record_from_row(row)

    def delete(self, incident_id: str) -> bool:
        normalized_id = _require_non_blank(
            incident_id,
            field_name="incident_id",
        )

        try:
            with self._connection_factory() as connection:
                result = connection.execute(
                    """
                    DELETE FROM incident_agent_app.incidents
                    WHERE incident_id = %s
                    RETURNING incident_id
                    """,
                    (normalized_id,),
                )
                row = result.fetchone()

        except psycopg.Error as error:
            raise IncidentRepositoryError(
                "failed to delete incident metadata"
            ) from error

        return row is not None