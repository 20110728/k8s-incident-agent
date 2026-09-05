from fastapi.testclient import TestClient

from backend.app.api import (
    dependencies as api_dependencies,
)
from backend.app.api.dependencies import (
    get_incident_service,
)
from backend.app.config import ApiSettings
from backend.app.main import create_app
from backend.app.services.incident_service import (
    IncidentApprovalConflictError,
    IncidentGraphError,
    IncidentNotAwaitingApprovalError,
    IncidentNotFoundError,
    IncidentSnapshot,
    IncidentServiceError,
)
from backend.tests.api.fakes import (
    FakeIncidentService,
)


INCIDENT_ID = "incident-api-test"
INCIDENT_PATH = (
    f"/api/v1/incidents/{INCIDENT_ID}"
)
APPROVAL_PATH = f"{INCIDENT_PATH}/approval"
APPROVAL_ID = "apr-0123456789abcdef"


def incident_state() -> dict:
    return {
        "incident_id": INCIDENT_ID,
        "request": {
            "namespace": "agent-demo",
            "service_name": "order-service",
            "description": "Service没有可用端点",
        },
        "phase": "awaiting_approval",
        "valid": True,
        "error_count": 0,
        "collection_plan": [],
        "evidence": [],
        "retrieved_runbooks": [],
        "diagnosis": None,
        "remediation_plan": None,
        "requires_approval": True,
        "approved": None,
        "approval_status": "pending",
        "approval_request": None,
        "approval_record": None,
        "action_result": None,
        "verification_result": None,
        "errors": [],
        "trace": [],
    }


def incident_snapshot() -> IncidentSnapshot:
    return IncidentSnapshot(
        incident_id=INCIDENT_ID,
        thread_id=INCIDENT_ID,
        state=incident_state(),
    )


def approval_snapshot(
    *,
    approved: bool,
) -> IncidentSnapshot:
    state = incident_state()
    state.update(
        {
            "phase": (
                "recovery_verified"
                if approved
                else "approval_rejected"
            ),
            "approved": approved,
            "approval_status": (
                "approved" if approved else "rejected"
            ),
        }
    )
    return IncidentSnapshot(
        incident_id=INCIDENT_ID,
        thread_id=INCIDENT_ID,
        state=state,
    )


def approval_payload(
    *,
    approved: bool = True,
) -> dict:
    return {
        "approval_id": APPROVAL_ID,
        "approved": approved,
        "approver": "test-operator",
        "comment": "reviewed",
    }


def make_test_settings() -> ApiSettings:
    return ApiSettings(
        _env_file=None,
        environment="test",
    )


def client_for(
    service: FakeIncidentService,
) -> TestClient:
    app = create_app(make_test_settings())
    app.dependency_overrides[
        get_incident_service
    ] = lambda: service

    return TestClient(app)


def test_health_does_not_build_real_graph(
    monkeypatch,
) -> None:
    def fail_if_called():
        raise AssertionError(
            "real graph must be built lazily"
        )

    monkeypatch.setattr(
        api_dependencies,
        "build_incident_service",
        fail_if_called,
    )
    app = create_app(make_test_settings())

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200


def test_create_incident_returns_accepted_state() -> None:
    service = FakeIncidentService(
        create_result=incident_snapshot()
    )

    with client_for(service) as client:
        response = client.post(
            "/api/v1/incidents",
            json={
                "namespace": "agent-demo",
                "service_name": "order-service",
                "description": (
                    "Service没有可用端点"
                ),
            },
        )

    assert response.status_code == 202
    body = response.json()
    assert body["incident_id"] == INCIDENT_ID
    assert body["thread_id"] == INCIDENT_ID
    assert body["phase"] == "awaiting_approval"
    assert body["waiting_for_approval"] is True

    assert len(service.create_calls) == 1
    request = service.create_calls[0]
    assert request.namespace == "agent-demo"
    assert request.service_name == "order-service"


def test_get_incident_returns_current_state() -> None:
    service = FakeIncidentService(
        get_result=incident_snapshot()
    )

    with client_for(service) as client:
        response = client.get(INCIDENT_PATH)

    assert response.status_code == 200
    assert response.json()["phase"] == (
        "awaiting_approval"
    )
    assert service.get_calls == [INCIDENT_ID]


def test_invalid_request_uses_standard_error() -> None:
    service = FakeIncidentService()

    with client_for(service) as client:
        response = client.post(
            "/api/v1/incidents",
            json={
                "namespace": "agent-demo",
                "service_name": "Order_Service",
                "description": "服务异常",
                "shell_command": "forbidden",
            },
        )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == (
        "REQUEST_VALIDATION_ERROR"
    )
    assert service.create_calls == []


def test_unknown_incident_returns_404() -> None:
    service = FakeIncidentService(
        get_error=IncidentNotFoundError(
            "incident was not found"
        )
    )

    with client_for(service) as client:
        response = client.get(INCIDENT_PATH)

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "INCIDENT_NOT_FOUND",
            "message": (
                "The requested incident was not found."
            ),
            "details": None,
        }
    }


def test_create_graph_failure_returns_502() -> None:
    service = FakeIncidentService(
        create_error=IncidentGraphError(
            "fake graph failure"
        )
    )

    with client_for(service) as client:
        response = client.post(
            "/api/v1/incidents",
            json={
                "namespace": "agent-demo",
                "service_name": "order-service",
                "description": "服务异常",
            },
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == (
        "INCIDENT_PROCESSING_FAILED"
    )


def test_get_graph_failure_returns_502() -> None:
    service = FakeIncidentService(
        get_error=IncidentGraphError(
            "fake checkpoint failure"
        )
    )

    with client_for(service) as client:
        response = client.get(INCIDENT_PATH)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == (
        "INCIDENT_PROCESSING_FAILED"
    )

def test_get_persistence_failure_returns_500() -> None:
    service = FakeIncidentService(
        get_error=IncidentServiceError(
            "fake persistence failure"
        )
    )

    with client_for(service) as client:
        response = client.get(INCIDENT_PATH)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == (
        "INCIDENT_SERVICE_ERROR"
    )

def test_invalid_incident_id_is_rejected() -> None:
    service = FakeIncidentService()

    with client_for(service) as client:
        response = client.get(
            "/api/v1/incidents/invalid_id"
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == (
        "REQUEST_VALIDATION_ERROR"
    )
    assert service.get_calls == []


def test_submit_approval_returns_current_state() -> None:
    service = FakeIncidentService(
        approval_result=approval_snapshot(
            approved=True
        )
    )

    with client_for(service) as client:
        response = client.post(
            APPROVAL_PATH,
            json=approval_payload(),
        )

    assert response.status_code == 200
    assert response.json()["phase"] == (
        "recovery_verified"
    )
    assert response.json()["approved"] is True
    assert len(service.approval_calls) == 1
    incident_id, decision = service.approval_calls[0]
    assert incident_id == INCIDENT_ID
    assert decision.approval_id == APPROVAL_ID
    assert decision.approver == "test-operator"


def test_submit_rejection_returns_rejected_state() -> None:
    service = FakeIncidentService(
        approval_result=approval_snapshot(
            approved=False
        )
    )

    with client_for(service) as client:
        response = client.post(
            APPROVAL_PATH,
            json=approval_payload(approved=False),
        )

    assert response.status_code == 200
    assert response.json()["phase"] == (
        "approval_rejected"
    )
    assert response.json()["approved"] is False


def test_invalid_approval_payload_is_rejected() -> None:
    service = FakeIncidentService()
    payload = approval_payload()
    payload["approved"] = "true"
    payload["unexpected"] = "forbidden"

    with client_for(service) as client:
        response = client.post(
            APPROVAL_PATH,
            json=payload,
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == (
        "REQUEST_VALIDATION_ERROR"
    )
    assert service.approval_calls == []


def test_invalid_approval_incident_id_is_rejected() -> None:
    service = FakeIncidentService()

    with client_for(service) as client:
        response = client.post(
            "/api/v1/incidents/invalid_id/approval",
            json=approval_payload(),
        )

    assert response.status_code == 422
    assert service.approval_calls == []


def test_approval_unknown_incident_returns_404() -> None:
    service = FakeIncidentService(
        approval_error=IncidentNotFoundError(
            "incident was not found"
        )
    )

    with client_for(service) as client:
        response = client.post(
            APPROVAL_PATH,
            json=approval_payload(),
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == (
        "INCIDENT_NOT_FOUND"
    )


def test_incident_not_awaiting_approval_returns_409() -> None:
    service = FakeIncidentService(
        approval_error=IncidentNotAwaitingApprovalError(
            "incident is not awaiting approval"
        )
    )

    with client_for(service) as client:
        response = client.post(
            APPROVAL_PATH,
            json=approval_payload(),
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == (
        "INCIDENT_NOT_AWAITING_APPROVAL"
    )


def test_approval_conflict_returns_409() -> None:
    service = FakeIncidentService(
        approval_error=IncidentApprovalConflictError(
            "approval conflict"
        )
    )

    with client_for(service) as client:
        response = client.post(
            APPROVAL_PATH,
            json=approval_payload(),
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == (
        "APPROVAL_CONFLICT"
    )


def test_approval_graph_failure_returns_502() -> None:
    service = FakeIncidentService(
        approval_error=IncidentGraphError(
            "fake graph failure"
        )
    )

    with client_for(service) as client:
        response = client.post(
            APPROVAL_PATH,
            json=approval_payload(),
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == (
        "INCIDENT_PROCESSING_FAILED"
    )


def test_approval_service_failure_returns_500() -> None:
    service = FakeIncidentService(
        approval_error=IncidentServiceError(
            "fake persistence failure"
        )
    )

    with client_for(service) as client:
        response = client.post(
            APPROVAL_PATH,
            json=approval_payload(),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == (
        "INCIDENT_SERVICE_ERROR"
    )


def test_openapi_contains_incident_contracts() -> None:
    service = FakeIncidentService()

    with client_for(service) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/incidents" in paths
    assert (
        "/api/v1/incidents/{incident_id}"
        in paths
    )
    assert "post" in paths["/api/v1/incidents"]
    assert (
        "get"
        in paths[
            "/api/v1/incidents/{incident_id}"
        ]
    )
    approval_path = (
        "/api/v1/incidents/{incident_id}/approval"
    )
    assert approval_path in paths
    assert "post" in paths[approval_path]
