from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from backend.app.agent.graph import (
    build_incident_graph,
)
from backend.app.agent.schemas import (
    Diagnosis,
    LabelPair,
    RemediationParameters,
    RemediationPlan,
)
from backend.tests.agent.fakes import (
    FakeCollector,
    FakeDiagnoser,
    FakeRemediationPlanner,
    FakeRetriever,
)


def selector_mismatch_bundle() -> dict:
    return {
        "namespace": "agent-demo",
        "service_name": "order-service",
        "service": {
            "namespace": "agent-demo",
            "name": "order-service",
            "service_type": "ClusterIP",
            "cluster_ip": "10.96.23.145",
            "selector": {
                "app": "wrong-order-service",
            },
            "ports": [
                {
                    "name": "http",
                    "port": 80,
                    "target_port": 8080,
                }
            ],
        },
        "service_pod_names": [],
        "namespace_pod_names": [
            "order-service-test",
        ],
        "endpoint_slices": [],
        "pod_statuses": {
            "order-service-test": {
                "phase": "Running",
                "ready": True,
                "labels": {
                    "app": "order-service",
                },
            }
        },
        "pod_events": {
            "order-service-test": [],
        },
        "pod_logs": [],
        "owner_chains": {},
        "deployments": {},
        "nodes": {},
        "errors": [],
    }


def selector_runbooks() -> list[dict]:
    return [
        {
            "document_id": "doc-selector-001",
            "runbook_id": "selector-label-mismatch",
            "category": "selector",
            "title": "Service Selector与Pod标签不匹配",
            "section": "修复步骤",
            "source": (
                "selector/"
                "selector-label-mismatch.md"
            ),
            "chunk_index": 0,
            "content": (
                "比较Service Selector和Pod标签，"
                "确认后修正Service Selector。"
            ),
            "score": 0.01,
        }
    ]


def selector_diagnosis() -> Diagnosis:
    # incident_id固定为day13-selector-approval，
    # normalize_evidence生成ev-day13-xxx。
    return Diagnosis(
        fault_category="service_selector_mismatch",
        root_cause=(
            "Service Selector中的app标签值"
            "与Ready Pod标签值不一致。"
        ),
        evidence_ids=[
            "ev-day13-001",
            "ev-day13-002",
            "ev-day13-003",
        ],
        runbook_ids=[
            "selector-label-mismatch",
        ],
        confidence=0.96,
        reasoning_summary=(
            "Service没有选中任何Pod，"
            "但命名空间中存在标签不同的Ready Pod。"
        ),
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
        summary=(
            "将Service Selector修正为Ready Pod使用的标签。"
        ),
        expected_result=(
            "Service重新选中Pod并生成Ready Endpoint。"
        ),
        rollback_plan=(
            "将Service Selector恢复为原标签值。"
        ),
        evidence_ids=[
            "ev-day13-001",
            "ev-day13-002",
            "ev-day13-003",
        ],
        runbook_ids=[
            "selector-label-mismatch",
        ],
        requires_approval=True,
    )


def manual_plan() -> RemediationPlan:
    return RemediationPlan(
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
                "人工确认Service Selector和Pod标签。",
                "确认变更窗口和影响范围。",
            ],
        ),
        risk_level="low",
        summary="继续人工检查Selector配置。",
        expected_result="确认是否需要修改Service。",
        rollback_plan="未执行修改，无需回滚。",
        evidence_ids=[
            "ev-day13-001",
            "ev-day13-002",
            "ev-day13-003",
        ],
        runbook_ids=[
            "selector-label-mismatch",
        ],
        requires_approval=False,
    )


def build_test_graph(
    remediation_plan: RemediationPlan,
):
    collector = FakeCollector(
        result=selector_mismatch_bundle()
    )
    retriever = FakeRetriever(
        result=selector_runbooks()
    )
    diagnoser = FakeDiagnoser(
        diagnosis=selector_diagnosis()
    )
    planner = FakeRemediationPlanner(
        plan=remediation_plan
    )

    graph = build_incident_graph(
        collector=collector,
        retriever=retriever,
        diagnoser=diagnoser,
        planner=planner,
        checkpointer=InMemorySaver(),
    )

    return (
        graph,
        collector,
        retriever,
        diagnoser,
        planner,
    )


def initial_state() -> dict:
    return {
        "incident_id": "day13-selector-approval",
        "request": {
            "namespace": "agent-demo",
            "service_name": "order-service",
            "description": (
                "Service Selector无法匹配Pod标签"
            ),
        },
    }


@pytest.mark.parametrize(
    ("approved", "expected_phase", "expected_status"),
    [
        (
            True,
            "approval_approved",
            "approved",
        ),
        (
            False,
            "approval_rejected",
            "rejected",
        ),
    ],
)
def test_full_graph_interrupts_and_resumes(
    approved: bool,
    expected_phase: str,
    expected_status: str,
):
    (
        graph,
        collector,
        retriever,
        diagnoser,
        planner,
    ) = build_test_graph(
        selector_patch_plan()
    )

    config = {
        "configurable": {
            "thread_id": str(uuid4()),
        }
    }

    paused = graph.invoke(
        initial_state(),
        config=config,
    )

    assert paused["phase"] == "awaiting_approval"
    assert paused["approval_status"] == "pending"
    assert paused["approved"] is None
    assert "__interrupt__" in paused

    interrupt_value = paused[
        "__interrupt__"
    ][0].value

    assert (
        interrupt_value["type"]
        == "remediation_approval_required"
    )

    request = interrupt_value[
        "approval_request"
    ]

    assert request["incident_id"] == (
        "day13-selector-approval"
    )
    assert request["plan"]["action"] == (
        "patch_service_selector"
    )

    result = graph.invoke(
        Command(
            resume={
                "approval_id": request[
                    "approval_id"
                ],
                "approved": approved,
                "approver": "fake-operator",
                "comment": "Day 13端到端测试",
            }
        ),
        config=config,
    )

    assert result["phase"] == expected_phase
    assert result["approval_status"] == (
        expected_status
    )
    assert result["approved"] is approved
    assert result["errors"] == []

    record = result["approval_record"]

    assert record.approved is approved
    assert record.approver == "fake-operator"
    assert record.approval_id == (
        request["approval_id"]
    )

    # 审批恢复不得重新执行上游节点。
    assert collector.calls == [
        (
            "agent-demo",
            "order-service",
        )
    ]
    assert len(retriever.queries) == 1
    assert len(diagnoser.calls) == 1
    assert len(planner.calls) == 1

    # Day 13不允许进入实际执行。
    assert result.get("action_result") is None


def test_manual_plan_finishes_without_interrupt():
    (
        graph,
        collector,
        retriever,
        diagnoser,
        planner,
    ) = build_test_graph(
        manual_plan()
    )

    config = {
        "configurable": {
            "thread_id": str(uuid4()),
        }
    }

    result = graph.invoke(
        initial_state(),
        config=config,
    )

    assert "__interrupt__" not in result
    assert result["phase"] == "remediation_planned"
    assert result["requires_approval"] is False
    assert result["approved"] is None
    assert result.get("approval_record") is None
    assert result.get("action_result") is None

    assert len(collector.calls) == 1
    assert len(retriever.queries) == 1
    assert len(diagnoser.calls) == 1
    assert len(planner.calls) == 1