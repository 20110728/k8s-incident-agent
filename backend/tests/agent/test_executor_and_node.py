from types import SimpleNamespace

import pytest

from backend.app.agent import (
    executor as executor_module,
)
from backend.app.agent.execution_policy import (
    ExecutionAuthorization,
    InvalidExecutionAuthorization,
)
from backend.app.agent.executor import (
    KubernetesRemediationExecutor,
)
from backend.app.agent.nodes import (
    make_execute_remediation_node,
)
from backend.app.agent.schemas import (
    ActionExecutionResult,
    ApprovalRecord,
    ApprovalRequest,
    LabelPair,
    RemediationParameters,
    RemediationPlan,
    ResourceMutationResult,
    ResourceSnapshot,
)
from backend.tests.agent.fakes import (
    FakeRemediationExecutor,
)


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
        expected_result="Service恢复Ready端点。",
        rollback_plan="恢复原Selector。",
        evidence_ids=["ev-test-001"],
        runbook_ids=[
            "selector-label-mismatch"
        ],
        requires_approval=True,
    )


def readiness_plan() -> RemediationPlan:
    return RemediationPlan(
        action="patch_readiness_probe",
        parameters=RemediationParameters(
            namespace="agent-demo",
            resource_kind="Deployment",
            resource_name="order-service",
            container_name="order-service",
            current_probe_path=(
                "/wrong-health"
            ),
            proposed_probe_path="/healthz",
            current_probe_port="http",
            proposed_probe_port="http",
            current_selector=[],
            proposed_selector=[],
            investigation_steps=[],
        ),
        risk_level="medium",
        summary="修正Readiness Probe。",
        expected_result="Pod恢复Ready。",
        rollback_plan="恢复原Probe。",
        evidence_ids=["ev-test-001"],
        runbook_ids=["wrong-http-path"],
        requires_approval=True,
    )


def authorization_for(
    plan: RemediationPlan,
    *,
    already_executed: bool = False,
    previous_result: (
        ActionExecutionResult | None
    ) = None,
) -> ExecutionAuthorization:
    request = ApprovalRequest(
        approval_id=(
            "apr-0123456789abcdef"
        ),
        incident_id="incident-test",
        plan=plan,
    )
    record = ApprovalRecord(
        approval_id=request.approval_id,
        incident_id=request.incident_id,
        action=plan.action,
        approved=True,
        approver="test-operator",
        comment="批准",
        decided_at=(
            "2026-09-03T12:00:00+00:00"
        ),
    )

    return ExecutionAuthorization(
        execution_id=(
            "exec-0123456789abcdef"
        ),
        approval_id=request.approval_id,
        incident_id=request.incident_id,
        plan=plan,
        approval_request=request,
        approval_record=record,
        previous_result=previous_result,
        already_executed=already_executed,
    )


def snapshot(
    *,
    kind: str,
    version: str,
    configuration: dict,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        namespace="agent-demo",
        resource_kind=kind,
        resource_name="order-service",
        resource_version=version,
        configuration=configuration,
    )


def successful_mutation(
    *,
    kind: str,
) -> ResourceMutationResult:
    return ResourceMutationResult(
        status="succeeded",
        before_snapshot=snapshot(
            kind=kind,
            version="rv-1",
            configuration={"before": True},
        ),
        after_snapshot=snapshot(
            kind=kind,
            version="rv-2",
            configuration={"after": True},
        ),
        applied_patch={
            "metadata": {
                "resourceVersion": "rv-1",
            }
        },
        rollback_patch={
            "metadata": {
                "resourceVersion": "rv-2",
            }
        },
        message="Fake mutation succeeded.",
        error_code=None,
        error_message=None,
    )


def action_result(
    *,
    status: str,
    action: str = "patch_service_selector",
) -> ActionExecutionResult:
    kind = (
        "Service"
        if action == "patch_service_selector"
        else "Deployment"
    )

    return ActionExecutionResult(
        execution_id=(
            "exec-0123456789abcdef"
        ),
        approval_id=(
            "apr-0123456789abcdef"
        ),
        action=action,
        status=status,
        namespace="agent-demo",
        resource_kind=kind,
        resource_name="order-service",
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
        message="Fake action result.",
        error_code=(
            "FAKE_ERROR"
            if status in {
                "failed",
                "conflict",
            }
            else None
        ),
        error_message=(
            "Fake execution error."
            if status in {
                "failed",
                "conflict",
            }
            else None
        ),
    )


def time_source():
    values = iter(
        [
            "2026-09-03T12:01:00+00:00",
            "2026-09-03T12:01:01+00:00",
        ]
    )

    return lambda: next(values)


def test_executor_dispatches_service_selector(
    monkeypatch,
):
    plan = selector_plan()
    authorization = authorization_for(
        plan
    )
    calls: list[dict] = []

    monkeypatch.setattr(
        executor_module,
        "validate_execution_authorization",
        lambda state: authorization,
    )

    def fake_patch(**kwargs):
        calls.append(kwargs)
        return successful_mutation(
            kind="Service"
        )

    executor = KubernetesRemediationExecutor(
        clients=SimpleNamespace(),
        patch_service_selector_fn=fake_patch,
        now=time_source(),
    )

    result = executor.execute({})

    assert result.status == "succeeded"
    assert result.action == (
        "patch_service_selector"
    )
    assert result.started_at == (
        "2026-09-03T12:01:00+00:00"
    )
    assert result.finished_at == (
        "2026-09-03T12:01:01+00:00"
    )

    assert calls == [
        {
            "clients": executor._clients,
            "namespace": "agent-demo",
            "service_name": "order-service",
            "expected_selector": {
                "app": "wrong-service",
            },
            "proposed_selector": {
                "app": "order-service",
            },
        }
    ]


def test_executor_dispatches_readiness_probe(
    monkeypatch,
):
    plan = readiness_plan()
    authorization = authorization_for(
        plan
    )
    calls: list[dict] = []

    monkeypatch.setattr(
        executor_module,
        "validate_execution_authorization",
        lambda state: authorization,
    )

    def fake_patch(**kwargs):
        calls.append(kwargs)
        return successful_mutation(
            kind="Deployment"
        )

    executor = KubernetesRemediationExecutor(
        clients=SimpleNamespace(),
        patch_readiness_probe_fn=fake_patch,
        now=time_source(),
    )

    result = executor.execute({})

    assert result.status == "succeeded"
    assert result.action == (
        "patch_readiness_probe"
    )

    assert calls == [
        {
            "clients": executor._clients,
            "namespace": "agent-demo",
            "deployment_name": (
                "order-service"
            ),
            "container_name": "order-service",
            "expected_path": (
                "/wrong-health"
            ),
            "proposed_path": "/healthz",
            "expected_port": "http",
            "proposed_port": "http",
        }
    ]


def test_executor_returns_previous_result(
    monkeypatch,
):
    previous = action_result(
        status="succeeded"
    )
    authorization = authorization_for(
        selector_plan(),
        already_executed=True,
        previous_result=previous,
    )

    monkeypatch.setattr(
        executor_module,
        "validate_execution_authorization",
        lambda state: authorization,
    )

    calls: list[dict] = []

    def fake_patch(**kwargs):
        calls.append(kwargs)
        raise AssertionError(
            "patch must not be called"
        )

    executor = KubernetesRemediationExecutor(
        clients=SimpleNamespace(),
        patch_service_selector_fn=fake_patch,
    )

    result = executor.execute({})

    assert result == previous
    assert calls == []


def test_executor_converts_tool_exception(
    monkeypatch,
):
    authorization = authorization_for(
        selector_plan()
    )

    monkeypatch.setattr(
        executor_module,
        "validate_execution_authorization",
        lambda state: authorization,
    )

    def fake_patch(**kwargs):
        raise RuntimeError(
            "fake patch failure"
        )

    executor = KubernetesRemediationExecutor(
        clients=SimpleNamespace(),
        patch_service_selector_fn=fake_patch,
        now=time_source(),
    )

    result = executor.execute({})

    assert result.status == "failed"
    assert result.error_code == (
        "REMEDIATION_TOOL_ERROR"
    )
    assert result.error_message == (
        "fake patch failure"
    )


def test_execution_node_writes_success():
    fake = FakeRemediationExecutor(
        result=action_result(
            status="succeeded"
        )
    )
    node = make_execute_remediation_node(
        fake
    )

    result = node(
        {
            "error_count": 0,
        }
    )

    assert result["phase"] == (
        "remediation_executed"
    )
    assert result["action_result"].status == (
        "succeeded"
    )
    assert result.get("errors") is None
    assert len(fake.calls) == 1


def test_execution_node_writes_conflict():
    fake = FakeRemediationExecutor(
        result=action_result(
            status="conflict"
        )
    )
    node = make_execute_remediation_node(
        fake
    )

    result = node(
        {
            "error_count": 0,
        }
    )

    assert result["phase"] == (
        "remediation_execution_conflict"
    )
    assert result["error_count"] == 1
    assert result["errors"][0]["code"] == (
        "FAKE_ERROR"
    )
    assert len(fake.calls) == 1


def test_execution_node_writes_failure():
    fake = FakeRemediationExecutor(
        result=action_result(
            status="failed"
        )
    )
    node = make_execute_remediation_node(
        fake
    )

    result = node(
        {
            "error_count": 2,
        }
    )

    assert result["phase"] == (
        "remediation_execution_failed"
    )
    assert result["error_count"] == 3
    assert result["errors"][0]["code"] == (
        "FAKE_ERROR"
    )


def test_execution_node_rejects_invalid_authorization():
    fake = FakeRemediationExecutor(
        error=InvalidExecutionAuthorization(
            "approval is invalid"
        )
    )
    node = make_execute_remediation_node(
        fake
    )

    result = node(
        {
            "error_count": 0,
        }
    )

    assert result["phase"] == (
        "remediation_execution_failed"
    )
    assert result["error_count"] == 1
    assert result["errors"][0]["code"] == (
        "INVALID_EXECUTION_AUTHORIZATION"
    )
    assert "action_result" not in result