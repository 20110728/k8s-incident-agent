from datetime import UTC, datetime

import pytest
from psycopg import OperationalError
from psycopg.errors import UniqueViolation

from backend.app.persistence.incidents import (
    IncidentAlreadyExistsError,
    IncidentRepositoryError,
    NewIncidentRecord,
    PostgresIncidentRepository,
)
from backend.tests.persistence.fakes import (
    FakeConnection,
    FakeConnectionFactory,
)


INCIDENT_ID = "incident-persistence-test"
CREATED_AT = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)
UPDATED_AT = datetime(2026, 9, 4, 4, 1, tzinfo=UTC)


def incident_row(
    *,
    phase: str = "created",
) -> dict:
    return {
        "incident_id": INCIDENT_ID,
        "thread_id": INCIDENT_ID,
        "namespace": "agent-demo",
        "service_name": "order-service",
        "description": "Service没有可用端点",
        "phase": phase,
        "created_at": CREATED_AT,
        "updated_at": UPDATED_AT,
    }


def new_incident() -> NewIncidentRecord:
    return NewIncidentRecord(
        incident_id=INCIDENT_ID,
        thread_id=INCIDENT_ID,
        namespace="agent-demo",
        service_name="order-service",
        description="Service没有可用端点",
    )


def build_repository(
    connection: FakeConnection,
) -> PostgresIncidentRepository:
    return PostgresIncidentRepository(
        FakeConnectionFactory(connection)
    )


def test_create_incident_returns_persisted_record() -> None:
    connection = FakeConnection()
    connection.queue_result(incident_row())
    repository = build_repository(connection)

    record = repository.create(new_incident())

    assert record.incident_id == INCIDENT_ID
    assert record.thread_id == INCIDENT_ID
    assert record.phase == "created"
    assert connection.calls[0]["params"] == (
        INCIDENT_ID,
        INCIDENT_ID,
        "agent-demo",
        "order-service",
        "Service没有可用端点",
        "created",
    )
    assert "INSERT INTO" in connection.calls[0]["query"]


def test_get_incident_returns_record() -> None:
    connection = FakeConnection()
    connection.queue_result(
        incident_row(phase="awaiting_approval")
    )
    repository = build_repository(connection)

    record = repository.get(INCIDENT_ID)

    assert record is not None
    assert record.phase == "awaiting_approval"
    assert connection.calls[0]["params"] == (
        INCIDENT_ID,
    )


def test_get_unknown_incident_returns_none() -> None:
    connection = FakeConnection()
    connection.queue_result(None)
    repository = build_repository(connection)

    assert repository.get("missing-incident") is None


def test_update_phase_returns_updated_record() -> None:
    connection = FakeConnection()
    connection.queue_result(
        incident_row(phase="diagnosis_completed")
    )
    repository = build_repository(connection)

    record = repository.update_phase(
        INCIDENT_ID,
        "diagnosis_completed",
    )

    assert record is not None
    assert record.phase == "diagnosis_completed"
    assert connection.calls[0]["params"] == (
        "diagnosis_completed",
        INCIDENT_ID,
    )


def test_delete_incident_reports_if_row_existed() -> None:
    connection = FakeConnection()
    connection.queue_result(
        {"incident_id": INCIDENT_ID}
    )
    repository = build_repository(connection)

    assert repository.delete(INCIDENT_ID) is True


def test_delete_unknown_incident_returns_false() -> None:
    connection = FakeConnection()
    connection.queue_result(None)
    repository = build_repository(connection)

    assert repository.delete("missing-incident") is False


def test_duplicate_incident_is_mapped_to_domain_error() -> None:
    connection = FakeConnection()
    connection.execute_error = UniqueViolation(
        "fake duplicate"
    )
    repository = build_repository(connection)

    with pytest.raises(IncidentAlreadyExistsError):
        repository.create(new_incident())


def test_database_error_does_not_expose_connection_details() -> None:
    connection = FakeConnection()
    connection.execute_error = OperationalError(
        "fake database unavailable"
    )
    repository = build_repository(connection)

    with pytest.raises(
        IncidentRepositoryError,
        match="failed to read incident metadata",
    ):
        repository.get(INCIDENT_ID)


@pytest.mark.parametrize(
    ("field_name", "changes"),
    [
        ("incident_id", {"incident_id": " "}),
        ("thread_id", {"thread_id": " "}),
        ("namespace", {"namespace": " "}),
        ("service_name", {"service_name": " "}),
        ("description", {"description": " "}),
        ("phase", {"phase": " "}),
    ],
)
def test_create_rejects_blank_fields(
    field_name: str,
    changes: dict[str, str],
) -> None:
    connection = FakeConnection()
    repository = build_repository(connection)
    values = {
        **new_incident().__dict__,
        **changes,
    }

    with pytest.raises(
        ValueError,
        match=f"{field_name} must not be blank",
    ):
        repository.create(
            NewIncidentRecord(**values)
        )

    assert connection.calls == []