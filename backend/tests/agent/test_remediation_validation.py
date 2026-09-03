import pytest
from pydantic import ValidationError

from backend.app.agent.remediation_policy import (
    InvalidRemediationPlan,
    validate_remediation_plan,
    FORBIDDEN_COMMAND_PATTERN,
    get_allowed_remediation_actions,
)
from backend.app.agent.schemas import (
    LabelPair,
    RemediationParameters,
    RemediationPlan,
)




def selector_state() -> dict:
    return {
        "request": {
            "namespace": "agent-demo",
            "service_name": "order-service",
            "description": "Service没有可用端点",
        },
        "diagnosis": {
            "fault_category": (
                "service_selector_mismatch"
            ),
            "root_cause": "Service Selector错误",
            "evidence_ids": [
                "ev-test-001",
                "ev-test-002",
            ],
            "runbook_ids": [
                "selector-label-mismatch",
            ],
            "confidence": 0.95,
            "reasoning_summary": "Selector无法匹配Pod。",
        },
        "evidence": [
            {
                "evidence_id": "ev-test-001",
                "resource_type": "Service",
                "resource_name": "order-service",
                "data": {
                    "selector": {
                        "app": "wrong-service",
                    }
                },
            },
            {
                "evidence_id": "ev-test-002",
                "resource_type": "PodStatus",
                "resource_name": "order-service-abc",
                "data": {
                    "ready": True,
                    "labels": {
                        "app": "order-service",
                    },
                },
            },
        ],
        "retrieved_runbooks": [
            {
                "runbook_id": (
                    "selector-label-mismatch"
                ),
            }
        ],
    }


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
            "ev-test-001",
            "ev-test-002",
        ],
        runbook_ids=[
            "selector-label-mismatch",
        ],
        requires_approval=True,
    )


def test_valid_selector_plan_is_accepted():
    plan = selector_plan()

    result = validate_remediation_plan(
        plan=plan,
        state=selector_state(),
    )

    assert result == plan


def test_non_allowlisted_action_is_rejected_by_schema():
    data = selector_plan().model_dump()
    data["action"] = "delete_pod"

    with pytest.raises(ValidationError):
        RemediationPlan.model_validate(data)


def test_wrong_namespace_is_rejected():
    plan = selector_plan().model_copy(
        update={
            "parameters": (
                selector_plan().parameters.model_copy(
                    update={
                        "namespace": "default",
                    }
                )
            )
        }
    )

    with pytest.raises(
        InvalidRemediationPlan,
        match="namespace is not allowed",
    ):
        validate_remediation_plan(
            plan=plan,
            state=selector_state(),
        )


def test_unknown_target_is_rejected():
    plan = selector_plan().model_copy(
        update={
            "parameters": (
                selector_plan().parameters.model_copy(
                    update={
                        "resource_name": "other-service",
                    }
                )
            )
        }
    )

    with pytest.raises(
        InvalidRemediationPlan,
        match="target does not exist",
    ):
        validate_remediation_plan(
            plan=plan,
            state=selector_state(),
        )


def test_selector_must_match_evidenced_labels():
    plan = selector_plan().model_copy(
        update={
            "parameters": (
                selector_plan().parameters.model_copy(
                    update={
                        "proposed_selector": [
                            LabelPair(
                                key="app",
                                value="invented-service",
                            )
                        ]
                    }
                )
            )
        }
    )

    with pytest.raises(
        InvalidRemediationPlan,
        match="does not match any evidenced",
    ):
        validate_remediation_plan(
            plan=plan,
            state=selector_state(),
        )


def test_executable_action_requires_approval():
    plan = selector_plan().model_copy(
        update={
            "requires_approval": False,
        }
    )

    with pytest.raises(
        InvalidRemediationPlan,
        match="requires approval",
    ):
        validate_remediation_plan(
            plan=plan,
            state=selector_state(),
        )


def test_manual_plan_rejects_shell_command():
    plan = RemediationPlan(
        action="manual_investigation",
        parameters=RemediationParameters(
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
                "运行kubectl检查Pod日志。",
            ],
        ),
        risk_level="low",
        summary="继续人工检查。",
        expected_result="补充根因证据。",
        rollback_plan="未执行修改，无需回滚。",
        evidence_ids=[
            "ev-test-001",
        ],
        runbook_ids=[
            "selector-label-mismatch",
        ],
        requires_approval=False,
    )

    with pytest.raises(
        InvalidRemediationPlan,
        match="forbidden shell command",
    ):
        validate_remediation_plan(
            plan=plan,
            state=selector_state(),
        )

def test_inline_backticks_are_not_shell_commands():
    text = (
        "确认 `/wrong-health` 是否为实际健康检查路径。"
    )

    assert (
        FORBIDDEN_COMMAND_PATTERN.search(text)
        is None
    )


def test_code_block_is_forbidden():
    text = (
        "```shell\n"
        "some command\n"
        "```"
    )

    assert (
        FORBIDDEN_COMMAND_PATTERN.search(text)
        is not None
    )

def readiness_state(
    *,
    liveness_probe: dict | None = None,
) -> dict:
    return {
        "request": {
            "namespace": "agent-demo",
            "service_name": "order-service",
            "description": "Pod无法Ready",
        },
        "diagnosis": {
            "fault_category": (
                "readiness_probe_error"
            ),
            "root_cause": "探针路径错误",
            "evidence_ids": [
                "ev-test-001",
            ],
            "runbook_ids": [
                "wrong-http-path",
            ],
            "confidence": 0.95,
            "reasoning_summary": (
                "探针持续返回404。"
            ),
        },
        "evidence": [
            {
                "evidence_id": "ev-test-001",
                "resource_type": "Deployment",
                "resource_name": "order-service",
                "data": {
                    "containers": [
                        {
                            "name": "order-service",
                            "readiness_probe": {
                                "path": (
                                    "/wrong-health"
                                ),
                                "port": "http",
                            },
                            "liveness_probe": (
                                liveness_probe
                            ),
                        }
                    ]
                },
            }
        ],
        "retrieved_runbooks": [
            {
                "runbook_id": (
                    "wrong-http-path"
                ),
            }
        ],
    }


def readiness_patch_plan() -> RemediationPlan:
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
        summary="修正就绪探针路径。",
        expected_result=(
            "Pod恢复Ready状态。"
        ),
        rollback_plan=(
            "恢复修改前的探针路径。"
        ),
        evidence_ids=[
            "ev-test-001",
        ],
        runbook_ids=[
            "wrong-http-path",
        ],
        requires_approval=True,
    )


def test_readiness_without_candidate_only_allows_manual():
    actions = get_allowed_remediation_actions(
        readiness_state()
    )

    assert actions == {
        "manual_investigation",
    }


def test_readiness_with_liveness_candidate_allows_patch():
    actions = get_allowed_remediation_actions(
        readiness_state(
            liveness_probe={
                "path": "/healthz",
                "port": "http",
            }
        )
    )

    assert actions == {
        "manual_investigation",
        "patch_readiness_probe",
    }


def test_guessed_readiness_path_is_rejected():
    with pytest.raises(
        InvalidRemediationPlan,
        match="not allowed or not grounded",
    ):
        validate_remediation_plan(
            plan=readiness_patch_plan(),
            state=readiness_state(),
        )


def test_grounded_readiness_path_is_accepted():
    state = readiness_state(
        liveness_probe={
            "path": "/healthz",
            "port": "http",
        }
    )

    result = validate_remediation_plan(
        plan=readiness_patch_plan(),
        state=state,
    )

    assert result.action == (
        "patch_readiness_probe"
    )

