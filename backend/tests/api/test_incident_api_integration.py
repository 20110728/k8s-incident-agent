from fastapi.testclient import TestClient

from backend.app.api.dependencies import (
    get_incident_service,
)
from backend.app.config import ApiSettings
from backend.app.main import create_app
from backend.app.services.incident_service import (
    IncidentApplicationService,
)
from backend.tests.api.fakes import (
    FakeIncidentGraph,
)


INCIDENT_ID = "incident-layer-integration"


def test_http_create_then_get_uses_same_graph_thread() -> None:
    graph = FakeIncidentGraph(
        result={
            "phase": "evidence_collected",
            "valid": True,
            "error_count": 0,
            "collection_plan": [
                "get_service",
            ],
            "evidence": [],
            "retrieved_runbooks": [],
            "diagnosis": None,
            "remediation_plan": None,
            "requires_approval": False,
            "approved": None,
            "approval_status": None,
            "approval_request": None,
            "approval_record": None,
            "action_result": None,
            "verification_result": None,
            "errors": [],
            "trace": [],
        }
    )
    service = IncidentApplicationService(
        graph,
        id_factory=lambda: INCIDENT_ID,
    )
    settings = ApiSettings(
        _env_file=None,
        environment="test",
    )
    app = create_app(settings)
    app.dependency_overrides[
        get_incident_service
    ] = lambda: service

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/incidents",
            json={
                "namespace": "agent-demo",
                "service_name": "order-service",
                "description": "服务状态异常",
            },
        )
        loaded = client.get(
            f"/api/v1/incidents/{INCIDENT_ID}"
        )

    assert created.status_code == 202
    assert loaded.status_code == 200

    created_body = created.json()
    loaded_body = loaded.json()

    assert created_body["incident_id"] == INCIDENT_ID
    assert created_body["thread_id"] == INCIDENT_ID
    assert created_body["phase"] == (
        "evidence_collected"
    )
    assert loaded_body == created_body

    assert len(graph.invocations) == 1
    assert graph.invocations[0]["config"] == {
        "configurable": {
            "thread_id": INCIDENT_ID,
        }
    }
    assert graph.state_reads == [
        {
            "configurable": {
                "thread_id": INCIDENT_ID,
            }
        }
    ]