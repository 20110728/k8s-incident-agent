from backend.app.agent.nodes import (
    make_plan_remediation_node,
    skip_remediation,
)
from backend.app.agent.schemas import (
    RemediationParameters,
    RemediationPlan,
)
from backend.tests.agent.fakes import (
    FakeRemediationPlanner,
)


def remediation_state() -> dict:
    return {
        "request": {
            "namespace": "agent-demo",
            "service_name": "order-service",
            "description": "Pod反复重启",
        },
        "diagnosis": {
            "fault_category": (
                "crash_loop_backoff"
            ),
            "root_cause": "应用进程异常退出",
            "evidence_ids": [
                "ev-test-001",
            ],
            "runbook_ids": [
                "application-exit",
            ],
            "confidence": 0.9,
            "reasoning_summary": (
                "Pod状态和日志支持该结论。"
            ),
        },
        "evidence": [
            {
                "evidence_id": "ev-test-001",
                "resource_type": "PodStatus",
                "resource_name": "order-service-abc",
                "data": {
                    "phase": "Running",
                    "ready": False,
                },
            }
        ],
        "retrieved_runbooks": [
            {
                "runbook_id": "application-exit",
            }
        ],
        "errors": [],
        "error_count": 0,
    }


def manual_plan() -> RemediationPlan:
    return RemediationPlan(
        action="manual_investigation",
        parameters=RemediationParameters(
            namespace="agent-demo",
            resource_kind="Pod",
            resource_name="order-service-abc",
            container_name=None,
            current_probe_path=None,
            proposed_probe_path=None,
            current_probe_port=None,
            proposed_probe_port=None,
            current_selector=[],
            proposed_selector=[],
            investigation_steps=[
                "检查上一次容器终止日志。",
                "确认应用依赖服务是否可用。",
            ],
        ),
        risk_level="low",
        summary="继续人工定位应用退出原因。",
        expected_result=(
            "获得能够定位应用退出位置的证据。"
        ),
        rollback_plan=(
            "未执行自动修改，无需回滚。"
        ),
        evidence_ids=[
            "ev-test-001",
        ],
        runbook_ids=[
            "application-exit",
        ],
        requires_approval=False,
    )


def test_valid_plan_is_written_to_state():
    planner = FakeRemediationPlanner(
        plan=manual_plan()
    )

    node = make_plan_remediation_node(
        planner
    )
    result = node(remediation_state())

    assert result["phase"] == (
        "remediation_planned"
    )
    assert result["remediation_plan"][
        "action"
    ] == "manual_investigation"
    assert result["risk_level"] == "low"
    assert result["requires_approval"] is False
    assert result["approved"] is None

    assert result["remediation_llm_model"] == (
        "fake-remediation-model"
    )
    assert result["remediation_llm_usage"][
        "total_tokens"
    ] == 160


def test_invalid_plan_is_recorded():
    invalid_plan = manual_plan().model_copy(
        update={
            "requires_approval": True,
        }
    )

    planner = FakeRemediationPlanner(
        plan=invalid_plan
    )

    result = make_plan_remediation_node(
        planner
    )(remediation_state())

    assert result["phase"] == (
        "remediation_failed"
    )
    assert result["errors"][-1]["code"] == (
        "INVALID_REMEDIATION_PLAN"
    )


def test_planner_exception_is_recorded():
    planner = FakeRemediationPlanner(
        error=TimeoutError(
            "remediation model timed out"
        )
    )

    result = make_plan_remediation_node(
        planner
    )(remediation_state())

    assert result["phase"] == (
        "remediation_failed"
    )
    assert result["errors"][-1]["code"] == (
        "LLM_REMEDIATION_ERROR"
    )
    assert "timed out" in (
        result["errors"][-1]["message"]
    )


def test_skip_remediation_clears_plan_fields():
    state = remediation_state()
    state["diagnosis"]["fault_category"] = (
        "unknown"
    )

    result = skip_remediation(state)

    assert result["phase"] == (
        "remediation_skipped"
    )
    assert result["remediation_plan"] is None
    assert result["risk_level"] is None
    assert result["requires_approval"] is False
    assert result["approved"] is None