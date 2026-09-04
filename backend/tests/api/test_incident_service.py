import pytest

from backend.app.agent.schemas import (
    ApprovalRequest,
    IncidentRequest,
    LabelPair,
    RemediationParameters,
    RemediationPlan,
)
from backend.app.api.schemas import (
    CreateIncidentRequest,
    IncidentStatusResponse,
)
from backend.app.services.incident_service import (
    IncidentApplicationService,
    IncidentGraphError,
    IncidentNotFoundError,
)
from backend.tests.api.fakes import (
    FakeIncidentGraph,
)


INCIDENT_ID = "incident-api-test"


def incident_request() -> IncidentRequest:
    return IncidentRequest(
        namespace="agent-demo",
        service_name="order-service",
        description="Service没有可用端点",
    )


def selector_patch_plan() -> RemediationPlan:
    return RemediationPlan(
        action="patch_service_selector",
        parameters=RemediationParameters(
            namespace="agent-demo",
            resource_kind="Service",
            resource_name="order-service",
            container_name=None,
            current_probe_path=None,
            proposed_probe_path=None,
            current_probe_port=None,
            proposed_probe_port=None,
            current_selector=[
                LabelPair(
                    key="app",
                    value="wrong-order-service",
                )
            ],
            proposed_selector=[
                LabelPair(
                    key="app",
                    value="order-service",
                )
            ],
            investigation_steps=[],
        ),
        risk_level="medium",
        summary="修正Service Selector。",
        expected_result="Service恢复Ready端点。",
        rollback_plan="恢复原Service Selector。",
        evidence_ids=["ev-api-001"],
        runbook_ids=[
            "selector-label-mismatch"
        ],
        requires_approval=True,
    )


def pending_approval_result() -> dict:
    approval_request = ApprovalRequest(
        approval_id="apr-0123456789abcdef",
        incident_id=INCIDENT_ID,
        plan=selector_patch_plan(),
    )

    return {
        "phase": "awaiting_approval",
        "valid": True,
        "error_count": 0,
        "requires_approval": True,
        "approved": None,
        "approval_status": "pending",
        "approval_request": approval_request,
        "errors": [],
        "trace": [],
        "__interrupt__": (object(),),
    }


def build_service(
    graph: FakeIncidentGraph,
) -> IncidentApplicationService:
    return IncidentApplicationService(
        graph,
        id_factory=lambda: INCIDENT_ID,
    )


def test_create_incident_invokes_graph_with_ids() -> None:
    graph = FakeIncidentGraph(
        result=pending_approval_result()
    )
    service = build_service(graph)

    snapshot = service.create_incident(
        incident_request()
    )

    assert snapshot.incident_id == INCIDENT_ID
    assert snapshot.thread_id == INCIDENT_ID
    assert snapshot.phase == "awaiting_approval"
    assert snapshot.waiting_for_approval is True
    assert "__interrupt__" not in snapshot.state

    assert graph.invocations == [
        {
            "input": {
                "incident_id": INCIDENT_ID,
                "request": {
                    "namespace": "agent-demo",
                    "service_name": "order-service",
                    "description": (
                        "Service没有可用端点"
                    ),
                },
            },
            "config": {
                "configurable": {
                    "thread_id": INCIDENT_ID,
                }
            },
        }
    ]


def test_pydantic_state_is_json_normalized() -> None:
    graph = FakeIncidentGraph(
        result=pending_approval_result()
    )
    snapshot = build_service(
        graph
    ).create_incident(incident_request())

    approval_request = snapshot.state[
        "approval_request"
    ]

    assert isinstance(approval_request, dict)
    assert approval_request["approval_id"] == (
        "apr-0123456789abcdef"
    )
    assert approval_request["plan"]["action"] == (
        "patch_service_selector"
    )


def test_get_incident_uses_same_thread() -> None:
    graph = FakeIncidentGraph(
        result=pending_approval_result()
    )
    service = build_service(graph)
    service.create_incident(incident_request())

    snapshot = service.get_incident(INCIDENT_ID)

    assert snapshot.phase == "awaiting_approval"
    assert graph.state_reads == [
        {
            "configurable": {
                "thread_id": INCIDENT_ID,
            }
        }
    ]


def test_unknown_incident_is_rejected() -> None:
    service = build_service(FakeIncidentGraph())

    with pytest.raises(
        IncidentNotFoundError,
        match="was not found",
    ):
        service.get_incident("missing-incident")


def test_graph_invocation_error_is_wrapped() -> None:
    graph = FakeIncidentGraph(
        invoke_error=TimeoutError(
            "fake graph timeout"
        )
    )
    service = build_service(graph)

    with pytest.raises(
        IncidentGraphError,
        match="invocation failed",
    ):
        service.create_incident(
            incident_request()
        )

    with pytest.raises(IncidentNotFoundError):
        service.get_incident(INCIDENT_ID)


def test_graph_state_error_is_wrapped() -> None:
    graph = FakeIncidentGraph(
        result=pending_approval_result()
    )
    service = build_service(graph)
    service.create_incident(incident_request())
    graph.get_state_error = TimeoutError(
        "fake checkpoint timeout"
    )

    with pytest.raises(
        IncidentGraphError,
        match="state lookup failed",
    ):
        service.get_incident(INCIDENT_ID)


def test_status_response_contains_pending_plan() -> None:
    graph = FakeIncidentGraph(
        result=pending_approval_result()
    )
    snapshot = build_service(
        graph
    ).create_incident(incident_request())

    response = IncidentStatusResponse.from_state(
        incident_id=snapshot.incident_id,
        thread_id=snapshot.thread_id,
        state=snapshot.state,
        waiting_for_approval=(
            snapshot.waiting_for_approval
        ),
    )

    assert response.phase == "awaiting_approval"
    assert response.waiting_for_approval is True
    assert response.approval_status == "pending"
    assert response.approval_request is not None
    assert (
        response.approval_request.plan.action
        == "patch_service_selector"
    )


def test_create_request_rejects_extra_fields() -> None:
    with pytest.raises(
        ValueError,
        match="Extra inputs are not permitted",
    ):
        CreateIncidentRequest.model_validate(
            {
                "namespace": "agent-demo",
                "service_name": "order-service",
                "description": "服务异常",
                "shell_command": "forbidden",
            }
        )