from backend.tests.service_profiles.fixtures import with_profile
import pytest

from backend.app.agent.approval import (
    build_approval_request,
)
from backend.app.agent.execution_policy import (
    InvalidExecutionAuthorization,
    build_execution_id,
    validate_execution_authorization,
)
from backend.app.agent.schemas import (
    ActionExecutionResult,
    ApprovalRecord,
    Diagnosis,
    LabelPair,
    RemediationParameters,
    RemediationPlan,
)
from backend.app.agent.diagnosis_policy import validate_diagnosis_assessment
from backend.app.agent.remediation_policy import (
    InvalidRemediationPlan,
    validate_remediation_plan,
)
from backend.tests.agent.selector_fixtures import selector_assessment


def selector_plan() -> RemediationPlan:
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
                    value="wrong-service",
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
        expected_result=(
            "Service重新获得Ready端点。"
        ),
        rollback_plan=(
            "恢复修改前的Service Selector。"
        ),
        evidence_ids=[
            "ev-exec-001",
            "ev-exec-002",
            "ev-profile-deployment",
        ],
        runbook_ids=[
            "selector-label-mismatch",
        ],
        requires_approval=True,
    )


def selector_evidence() -> list[dict]:
    return [
        {
            "evidence_id": "ev-exec-001",
            "resource_type": "Service",
            "resource_name": "order-service",
            "data": {
                "namespace": "agent-demo",
                "name": "order-service",
                "selector": {
                    "app": "wrong-service",
                }
            },
        },
        {
            "evidence_id": "ev-exec-002",
            "resource_type": "PodStatus",
            "resource_name": "order-service-abc",
            "data": {
                "namespace": "agent-demo",
                "ready": True,
                "labels": {
                    "app": "order-service",
                },
            },
        },
        {
            "evidence_id": "ev-exec-owner",
            "resource_type": "OwnerChain",
            "resource_name": "order-service-abc",
            "data": {"owner_chain": {
                "namespace": "agent-demo", "deployment_name": "order-service",
            }},
        },
    ]


def approved_state() -> dict:
    plan = selector_plan()

    state = {
        "incident_id": "incident-execution-test",
        "request": {
            "namespace": "agent-demo",
            "service_name": "order-service",
            "description": (
                "Service Selector无法匹配Pod标签"
            ),
        },
        "phase": "remediation_planned",
        "requires_approval": True,
        "approved": None,
        "remediation_plan": plan,
        "diagnosis": {
            "assessment": selector_assessment(
                "ev-exec-001", "ev-profile-deployment"
            ).model_dump(),
            "fault_category": (
                "service_selector_mismatch"
            ),
            "root_cause": (
                "Service Selector与Pod标签不匹配。"
            ),
            "evidence_ids": [
                "ev-exec-001",
                "ev-exec-002",
                "ev-profile-deployment",
            ],
            "runbook_ids": [
                "selector-label-mismatch",
            ],
            "confidence": 0.95,
            "reasoning_summary": (
                "Service未选中Ready Pod。"
            ),
        },
        "evidence": selector_evidence(),
        "retrieved_runbooks": [
            {
                "runbook_id": (
                    "selector-label-mismatch"
                ),
            }
        ],
        "errors": [],
        "trace": [],
    }

    with_profile(state)
    deployment = next(
        e["data"] for e in state["evidence"]
        if e["evidence_id"] == "ev-profile-deployment"
    )
    deployment.update(desired_replicas=1, ready_replicas=1, available_replicas=1)
    deployment["containers"][0]["readiness_probe"] = {
        "path": "/healthz", "port": "http", "scheme": "HTTP",
    }
    approval_request = build_approval_request(
        state
    )

    approval_record = ApprovalRecord(
        approval_id=(
            approval_request.approval_id
        ),
        incident_id=state["incident_id"],
        action=plan.action,
        approved=True,
        approver="test-operator",
        comment="批准执行测试修复",
        decided_at=(
            "2026-09-03T12:00:00+00:00"
        ),
    )

    state.update(
        {
            "phase": "approval_approved",
            "approval_status": "approved",
            "approved": True,
            "approval_request": approval_request,
            "approval_record": approval_record,
        }
    )

    return state


def execution_result(
    state: dict,
    *,
    status: str,
) -> ActionExecutionResult:
    request = state["approval_request"]
    plan = state["remediation_plan"]

    return ActionExecutionResult(
        execution_id=build_execution_id(
            request.approval_id
        ),
        approval_id=request.approval_id,
        action=plan.action,
        status=status,
        namespace=plan.parameters.namespace,
        resource_kind=(
            plan.parameters.resource_kind
        ),
        resource_name=(
            plan.parameters.resource_name
        ),
        started_at=(
            "2026-09-03T12:01:00+00:00"
        ),
        finished_at=(
            "2026-09-03T12:01:01+00:00"
        ),
        before_snapshot=None,
        after_snapshot=None,
        applied_patch={},
        rollback_patch={},
        message="Fake execution result.",
        error_code=(
            "FAKE_ERROR"
            if status == "failed"
            else None
        ),
        error_message=(
            "Fake failure."
            if status == "failed"
            else None
        ),
    )


def test_execution_id_is_deterministic():
    approval_id = "apr-0123456789abcdef"

    assert (
        build_execution_id(approval_id)
        == build_execution_id(approval_id)
    )
    assert build_execution_id(
        approval_id
    ).startswith("exec-")


def test_invalid_approval_id_is_rejected():
    with pytest.raises(
        InvalidExecutionAuthorization,
        match="invalid format",
    ):
        build_execution_id("invalid-id")


def test_approved_plan_is_authorized():
    state = approved_state()

    result = validate_execution_authorization(
        state
    )

    assert result.plan.action == (
        "patch_service_selector"
    )
    assert result.already_executed is False
    assert result.previous_result is None
    assert result.execution_id.startswith(
        "exec-"
    )


def test_selector_fixture_satisfies_current_diagnosis_contract():
    state = approved_state()
    validate_diagnosis_assessment(Diagnosis.model_validate(state["diagnosis"]), state)
    validate_remediation_plan(plan=state["remediation_plan"], state=state)


@pytest.mark.parametrize("missing", ["assessment", "probe", "deployment_reference"])
def test_selector_fixture_missing_write_evidence_is_still_rejected(missing):
    state = approved_state()
    if missing == "assessment":
        state["diagnosis"].pop("assessment")
    elif missing == "probe":
        deployment = next(e["data"] for e in state["evidence"]
                          if e["resource_type"] == "Deployment")
        deployment["containers"][0].pop("readiness_probe")
    else:
        state["remediation_plan"].evidence_ids.remove("ev-profile-deployment")
    # Validate the policy directly so approval fingerprint rejection cannot mask
    # an accidentally weakened evidence gate.
    with pytest.raises(InvalidRemediationPlan):
        validate_remediation_plan(plan=state["remediation_plan"], state=state)


def test_rejected_state_is_not_authorized():
    state = approved_state()
    state["phase"] = "approval_rejected"
    state["approval_status"] = "rejected"
    state["approved"] = False

    with pytest.raises(
        InvalidExecutionAuthorization,
        match="phase is not approval_approved",
    ):
        validate_execution_authorization(
            state
        )


def test_changed_plan_is_not_authorized():
    state = approved_state()

    state["remediation_plan"] = (
        state["remediation_plan"].model_copy(
            update={
                "summary": (
                    "审批完成后被修改的方案"
                ),
            }
        )
    )

    with pytest.raises(
        InvalidExecutionAuthorization,
        match="plan changed after approval",
    ):
        validate_execution_authorization(
            state
        )


def test_mismatched_approval_ids_are_rejected():
    state = approved_state()

    state["approval_record"] = (
        state["approval_record"].model_copy(
            update={
                "approval_id": (
                    "apr-ffffffffffffffff"
                ),
            }
        )
    )

    with pytest.raises(
        InvalidExecutionAuthorization,
        match="approval IDs do not match",
    ):
        validate_execution_authorization(
            state
        )


def test_manual_action_is_not_executable():
    state = approved_state()
    old_plan = state["remediation_plan"]

    manual_parameters = (
        RemediationParameters(
            namespace="agent-demo",
            resource_kind="Service",
            resource_name="order-service",
            container_name=None,
            current_probe_path=None,
            proposed_probe_path=None,
            current_probe_port=None,
            proposed_probe_port=None,
            current_selector=[],
            proposed_selector=[],
            investigation_steps=[
                "继续人工检查Service配置。",
            ],
        )
    )

    manual_plan = old_plan.model_copy(
        update={
            "action": "manual_investigation",
            "parameters": manual_parameters,
            "risk_level": "low",
            "requires_approval": False,
        }
    )

    state["remediation_plan"] = manual_plan
    state["approval_request"] = (
        state["approval_request"].model_copy(
            update={
                "plan": manual_plan,
            }
        )
    )
    state["approval_record"] = (
        state["approval_record"].model_copy(
            update={
                "action": (
                    "manual_investigation"
                ),
            }
        )
    )

    with pytest.raises(
        InvalidExecutionAuthorization,
        match="is not executable",
    ):
        validate_execution_authorization(
            state
        )


@pytest.mark.parametrize(
    "status",
    [
        "succeeded",
        "already_applied",
    ],
)
def test_completed_execution_is_not_repeated(
    status: str,
):
    state = approved_state()
    state["action_result"] = execution_result(
        state,
        status=status,
    )

    result = validate_execution_authorization(
        state
    )

    assert result.already_executed is True
    assert result.previous_result is not None
    assert (
        result.previous_result.status
        == status
    )


def test_failed_execution_requires_new_approval():
    state = approved_state()
    state["action_result"] = execution_result(
        state,
        status="failed",
    )

    with pytest.raises(
        InvalidExecutionAuthorization,
        match="execution attempt has already",
    ):
        validate_execution_authorization(
            state
        )
