import pytest

from backend.app.agent.schemas import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    IncidentRequest,
    LabelPair,
    RemediationParameters,
    RemediationPlan,
)
from backend.app.persistence.incidents import (
    IncidentRepositoryError,
)
from backend.app.services.incident_service import (
    IncidentApplicationService,
    IncidentApprovalConflictError,
    IncidentGraphError,
    IncidentNotAwaitingApprovalError,
    IncidentNotFoundError,
    IncidentServiceError,
)
from backend.tests.api.fakes import (
    FakeIncidentGraph,
    FakeIncidentRepository,
)


INCIDENT_ID = "incident-approval-service"
APPROVAL_ID = "apr-0123456789abcdef"


def incident_request() -> IncidentRequest:
    return IncidentRequest(
        namespace="agent-demo",
        service_name="order-service",
        description="Service没有可用端点",
    )


def remediation_plan() -> RemediationPlan:
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
        runbook_ids=["selector-label-mismatch"],
        requires_approval=True,
    )


def pending_state() -> dict:
    return {
        "phase": "awaiting_approval",
        "valid": True,
        "requires_approval": True,
        "approved": None,
        "approval_status": "pending",
        "approval_request": ApprovalRequest(
            approval_id=APPROVAL_ID,
            incident_id=INCIDENT_ID,
            plan=remediation_plan(),
        ),
        "approval_record": None,
        "errors": [],
        "trace": [],
        "__interrupt__": (object(),),
    }


def decision(
    *,
    approved: bool = True,
    approval_id: str = APPROVAL_ID,
    approver: str = "test-operator",
    comment: str = "reviewed",
) -> ApprovalDecision:
    return ApprovalDecision(
        approval_id=approval_id,
        approved=approved,
        approver=approver,
        comment=comment,
    )


def resumed_state(
    submitted: ApprovalDecision,
    *,
    phase: str,
) -> dict:
    return {
        "phase": phase,
        "approval_status": (
            "approved" if submitted.approved else "rejected"
        ),
        "approved": submitted.approved,
        "approval_record": ApprovalRecord(
            approval_id=submitted.approval_id,
            incident_id=INCIDENT_ID,
            action="patch_service_selector",
            approved=submitted.approved,
            approver=submitted.approver,
            comment=submitted.comment,
            decided_at="2026-09-04T12:00:00+00:00",
        ),
    }


def build_started_service(
    *,
    resume_result: dict | None = None,
) -> tuple[
    IncidentApplicationService,
    FakeIncidentGraph,
    FakeIncidentRepository,
]:
    graph = FakeIncidentGraph(
        result=pending_state(),
        resume_result=resume_result,
    )
    repository = FakeIncidentRepository()
    service = IncidentApplicationService(
        graph,
        repository,
        id_factory=lambda: INCIDENT_ID,
    )
    service.create_incident(incident_request())
    return service, graph, repository


@pytest.mark.parametrize(
    ("approved", "expected_phase"),
    [
        (True, "recovery_verified"),
        (False, "approval_rejected"),
    ],
)
def test_submit_approval_resumes_graph_and_updates_phase(
    approved: bool,
    expected_phase: str,
) -> None:
    submitted = decision(approved=approved)
    service, graph, repository = build_started_service(
        resume_result=resumed_state(
            submitted,
            phase=expected_phase,
        )
    )

    snapshot = service.submit_approval(
        INCIDENT_ID,
        submitted,
    )

    assert snapshot.phase == expected_phase
    assert snapshot.state["approved"] is approved
    assert graph.resume_calls == [
        {
            "resume": submitted.model_dump(mode="json"),
            "config": {
                "configurable": {
                    "thread_id": INCIDENT_ID,
                }
            },
        }
    ]
    assert repository.update_phase_calls[-1] == (
        INCIDENT_ID,
        expected_phase,
    )


def test_identical_duplicate_decision_is_idempotent() -> None:
    submitted = decision()
    service, graph, repository = build_started_service(
        resume_result=resumed_state(
            submitted,
            phase="recovery_verified",
        )
    )

    first = service.submit_approval(INCIDENT_ID, submitted)
    update_count = len(repository.update_phase_calls)
    second = service.submit_approval(INCIDENT_ID, submitted)

    assert second.state == first.state
    assert len(graph.resume_calls) == 1
    assert len(repository.update_phase_calls) == update_count


def test_different_duplicate_decision_is_rejected() -> None:
    submitted = decision()
    service, graph, _ = build_started_service(
        resume_result=resumed_state(
            submitted,
            phase="recovery_verified",
        )
    )
    service.submit_approval(INCIDENT_ID, submitted)

    with pytest.raises(
        IncidentApprovalConflictError,
        match="already been decided differently",
    ):
        service.submit_approval(
            INCIDENT_ID,
            decision(approved=False),
        )

    assert len(graph.resume_calls) == 1


def test_mismatched_pending_approval_id_is_rejected() -> None:
    service, graph, _ = build_started_service()

    with pytest.raises(
        IncidentApprovalConflictError,
        match="does not match the pending request",
    ):
        service.submit_approval(
            INCIDENT_ID,
            decision(
                approval_id="apr-fedcba9876543210"
            ),
        )

    assert graph.resume_calls == []


def test_incident_without_pending_approval_is_rejected() -> None:
    graph = FakeIncidentGraph(
        result={
            "phase": "diagnosis_completed",
            "approval_status": "not_required",
            "approval_record": None,
        }
    )
    service = IncidentApplicationService(
        graph,
        id_factory=lambda: INCIDENT_ID,
    )
    service.create_incident(incident_request())

    with pytest.raises(
        IncidentNotAwaitingApprovalError,
        match="not awaiting approval",
    ):
        service.submit_approval(
            INCIDENT_ID,
            decision(),
        )


def test_unknown_incident_is_rejected() -> None:
    service = IncidentApplicationService(
        FakeIncidentGraph()
    )

    with pytest.raises(IncidentNotFoundError):
        service.submit_approval(
            "missing-incident",
            decision(),
        )


def test_graph_resume_failure_is_wrapped() -> None:
    service, graph, _ = build_started_service()
    graph.invoke_error = TimeoutError("fake graph timeout")

    with pytest.raises(
        IncidentGraphError,
        match="approval resume failed",
    ):
        service.submit_approval(
            INCIDENT_ID,
            decision(),
        )


def test_resume_without_approval_record_is_rejected() -> None:
    service, _, _ = build_started_service(
        resume_result={
            "phase": "approval_approved",
            "approved": True,
            "approval_status": "approved",
        }
    )

    with pytest.raises(
        IncidentGraphError,
        match="did not persist a valid approval record",
    ):
        service.submit_approval(
            INCIDENT_ID,
            decision(),
        )


def test_phase_update_failure_is_wrapped() -> None:
    submitted = decision()
    service, _, repository = build_started_service(
        resume_result=resumed_state(
            submitted,
            phase="recovery_verified",
        )
    )
    repository.update_phase_error = (
        IncidentRepositoryError("fake database failure")
    )

    with pytest.raises(
        IncidentServiceError,
        match="failed to update incident metadata after approval",
    ):
        service.submit_approval(
            INCIDENT_ID,
            submitted,
        )
