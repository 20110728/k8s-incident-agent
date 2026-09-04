import pytest

from backend.app.agent.schemas import IncidentRequest
from backend.app.persistence.incidents import (
    IncidentRepositoryError,
)
from backend.app.services.incident_service import (
    IncidentApplicationService,
    IncidentServiceError,
)
from backend.tests.api.fakes import (
    FakeIncidentGraph,
    FakeIncidentRepository,
)


INCIDENT_ID = "incident-persistent-service"


def incident_request() -> IncidentRequest:
    return IncidentRequest(
        namespace="agent-demo",
        service_name="order-service",
        description="Service没有可用端点",
    )


def build_graph() -> FakeIncidentGraph:
    return FakeIncidentGraph(
        result={
            "phase": "awaiting_approval",
            "approval_status": "pending",
        }
    )


def build_service(
    graph: FakeIncidentGraph,
    repository: FakeIncidentRepository,
) -> IncidentApplicationService:
    return IncidentApplicationService(
        graph,
        repository,
        id_factory=lambda: INCIDENT_ID,
    )


def test_create_persists_identity_and_phase() -> None:
    repository = FakeIncidentRepository()
    service = build_service(
        build_graph(),
        repository,
    )

    snapshot = service.create_incident(
        incident_request()
    )

    assert snapshot.incident_id == INCIDENT_ID
    assert repository.create_calls[0].thread_id == (
        INCIDENT_ID
    )
    assert repository.update_phase_calls == [
        (INCIDENT_ID, "awaiting_approval")
    ]


def test_new_service_instance_reads_persisted_mapping() -> None:
    graph = build_graph()
    repository = FakeIncidentRepository()
    build_service(
        graph,
        repository,
    ).create_incident(incident_request())

    restarted_service = build_service(
        graph,
        repository,
    )
    snapshot = restarted_service.get_incident(
        INCIDENT_ID
    )

    assert snapshot.thread_id == INCIDENT_ID
    assert repository.get_calls == [INCIDENT_ID]


def test_repository_failure_is_wrapped() -> None:
    repository = FakeIncidentRepository()
    repository.create_error = IncidentRepositoryError(
        "fake database failure"
    )
    service = build_service(
        build_graph(),
        repository,
    )

    with pytest.raises(
        IncidentServiceError,
        match="failed to persist incident metadata",
    ):
        service.create_incident(incident_request())