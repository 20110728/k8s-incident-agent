from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from backend.app.agent.approval import (
    InvalidApprovalRequest,
    build_approval_request,
)
from backend.app.agent.nodes import (
    prepare_approval,
    request_human_approval,
)
from backend.app.agent.schemas import (
    LabelPair,
    RemediationParameters,
    RemediationPlan,
)
from backend.app.agent.state import IncidentState


def approval_state() -> IncidentState:
    plan = RemediationPlan(
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
        expected_result="EndpointSlice恢复Ready端点。",
        rollback_plan="恢复原Service Selector。",
        evidence_ids=["ev-test-001"],
        runbook_ids=["selector-label-mismatch"],
        requires_approval=True,
    )

    return {
        "incident_id": "inc-approval-test",
        "phase": "remediation_planned",
        "remediation_plan": plan,
        "risk_level": "medium",
        "requires_approval": True,
        "approved": None,
        "errors": [],
        "trace": [],
    }


def compile_approval_graph():
    builder = StateGraph(IncidentState)
    builder.add_node(
        "request_human_approval",
        request_human_approval,
    )
    builder.add_edge(START, "request_human_approval")
    builder.add_edge("request_human_approval", END)

    return builder.compile(
        checkpointer=InMemorySaver(),
    )


def test_approval_id_is_deterministic():
    state = approval_state()

    first = build_approval_request(state)
    second = build_approval_request(state)

    assert first.approval_id == second.approval_id
    assert first.approval_id.startswith("apr-")


def test_manual_plan_cannot_create_approval_request():
    state = approval_state()
    plan = state["remediation_plan"].model_copy(
        update={
            "action": "manual_investigation",
            "requires_approval": True,
        }
    )
    state["remediation_plan"] = plan

    with pytest.raises(
        InvalidApprovalRequest,
        match="manual investigation",
    ):
        build_approval_request(state)


def test_prepare_approval_creates_pending_request():
    result = prepare_approval(approval_state())

    assert result["phase"] == "awaiting_approval"
    assert result["approval_status"] == "pending"
    assert result["approved"] is None
    assert result["approval_request"].approval_id.startswith(
        "apr-"
    )


@pytest.mark.parametrize(
    ("approved", "expected_phase", "expected_status"),
    [
        (True, "approval_approved", "approved"),
        (False, "approval_rejected", "rejected"),
    ],
)
def test_interrupt_and_resume_approval(
    approved,
    expected_phase,
    expected_status,
):
    graph = compile_approval_graph()

    initial_state = approval_state()
    prepared = prepare_approval(initial_state)
    initial_state.update(prepared)

    config = {
        "configurable": {
            "thread_id": str(uuid4()),
        }
    }

    paused = graph.invoke(initial_state, config=config)

    assert "__interrupt__" in paused
    interrupt_value = paused["__interrupt__"][0].value
    assert (
        interrupt_value["type"]
        == "remediation_approval_required"
    )

    approval_id = interrupt_value[
        "approval_request"
    ]["approval_id"]

    result = graph.invoke(
        Command(
            resume={
                "approval_id": approval_id,
                "approved": approved,
                "approver": "day13-tester",
                "comment": "人工验收结果",
            }
        ),
        config=config,
    )

    assert result["phase"] == expected_phase
    assert result["approval_status"] == expected_status
    assert result["approved"] is approved
    assert result["approval_record"].approved is approved
    assert (
        result["approval_record"].approval_id
        == approval_id
    )
    assert result["approval_record"].decided_at


def test_mismatched_approval_id_is_rejected():
    graph = compile_approval_graph()

    initial_state = approval_state()
    initial_state.update(
        prepare_approval(initial_state)
    )

    config = {
        "configurable": {
            "thread_id": str(uuid4()),
        }
    }

    paused = graph.invoke(initial_state, config=config)
    assert "__interrupt__" in paused

    result = graph.invoke(
        Command(
            resume={
                "approval_id": "apr-0000000000000000",
                "approved": True,
                "approver": "day13-tester",
                "comment": "错误的审批ID",
            }
        ),
        config=config,
    )

    assert result["phase"] == "approval_failed"
    assert result["approval_status"] == "failed"
    assert result["approved"] is None
    assert result["errors"][-1]["code"] == (
        "INVALID_APPROVAL_DECISION"
    )


def test_existing_record_prevents_duplicate_approval():
    graph = compile_approval_graph()

    initial_state = approval_state()
    initial_state.update(
        prepare_approval(initial_state)
    )

    config = {
        "configurable": {
            "thread_id": str(uuid4()),
        }
    }

    paused = graph.invoke(initial_state, config=config)
    approval_id = paused[
        "__interrupt__"
    ][0].value["approval_request"]["approval_id"]

    approved_state = graph.invoke(
        Command(
            resume={
                "approval_id": approval_id,
                "approved": True,
                "approver": "day13-tester",
                "comment": "批准",
            }
        ),
        config=config,
    )

    duplicate_result = request_human_approval(
        approved_state
    )

    assert duplicate_result["phase"] == (
        "approval_approved"
    )
    assert duplicate_result["approved"] is True
    assert (
        duplicate_result["trace"][0]["message"]
        == "duplicate approval request ignored"
    )