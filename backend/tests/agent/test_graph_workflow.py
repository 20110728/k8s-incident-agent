import pytest

from backend.app.agent.graph import build_incident_graph
from backend.app.agent.schemas import (
    Diagnosis,
    RemediationParameters,
    RemediationPlan,
)
from backend.app.llm.diagnoser import DiagnosisCallResult
from backend.tests.agent.fakes import (
    FakeCollector,
    FakeRetriever,
    FakeRemediationPlanner,
)
from backend.app.llm.remediation_planner import (
    RemediationCallResult,
)

def healthy_bundle() -> dict:
    return {
        "namespace": "agent-demo",
        "service_name": "order-service",
        "service": {
            "namespace": "agent-demo",
            "name": "order-service",
            "service_type": "ClusterIP",
            "cluster_ip": "10.96.23.145",
            "selector": {
                "app": "order-service",
            },
            "ports": [],
        },
        "service_pod_names": [
            "order-service-abc",
        ],
        "namespace_pod_names": [
            "order-service-abc",
        ],
        "endpoint_slices": [],
        "pod_statuses": {
            "order-service-abc": {
                "phase": "Running",
                "ready": True,
            }
        },
        "pod_events": {
            "order-service-abc": [],
        },
        "pod_logs": [],
        "owner_chains": {},
        "deployments": {},
        "nodes": {},
        "errors": [],
    }


def runbook_result() -> list[dict]:
    return [
        {
            "document_id": "doc-001",
            "runbook_id": "wrong-http-path",
            "category": "readiness",
            "title": "Readiness Probe路径错误",
            "section": "判断规则",
            "source": "readiness/wrong-http-path.md",
            "chunk_index": 0,
            "content": "检查Readiness Probe的HTTP路径。",
            "score": 0.1,
        }
    ]


class StateAwareDiagnoser:
    """根据运行时State中的真实ID生成Fake诊断。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def diagnose(
        self,
        state: dict,
    ) -> DiagnosisCallResult:
        self.calls.append(state)

        evidence_id = state["evidence"][0][
            "evidence_id"
        ]
        runbook_id = state["retrieved_runbooks"][0][
            "runbook_id"
        ]

        return DiagnosisCallResult(
            diagnosis=Diagnosis(
                fault_category=(
                    "readiness_probe_error"
                ),
                root_cause="Readiness Probe路径错误",
                evidence_ids=[evidence_id],
                runbook_ids=[runbook_id],
                confidence=0.9,
                reasoning_summary=(
                    "集群证据与Runbook支持该结论。"
                ),
            ),
            model_name="fake-model",
            usage={
                "input_tokens": 100,
                "output_tokens": 30,
                "total_tokens": 130,
            },
        )

class StateAwareRemediationPlanner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def plan(
        self,
        state: dict,
    ) -> RemediationCallResult:
        self.calls.append(state)

        evidence_id = state["diagnosis"][
            "evidence_ids"
        ][0]
        runbook_id = state["diagnosis"][
            "runbook_ids"
        ][0]

        return RemediationCallResult(
            plan=RemediationPlan(
                action="manual_investigation",
                parameters=(
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
                            (
                                "检查应用实际健康检查"
                                "端点配置。"
                            )
                        ],
                    )
                ),
                risk_level="low",
                summary=(
                    "继续人工确认探针目标。"
                ),
                expected_result=(
                    "获得正确健康检查端点证据。"
                ),
                rollback_plan=(
                    "未执行自动修改，无需回滚。"
                ),
                evidence_ids=[evidence_id],
                runbook_ids=[runbook_id],
                requires_approval=False,
            ),
            model_name="fake-remediation-model",
            usage={
                "input_tokens": 120,
                "output_tokens": 40,
                "total_tokens": 160,
            },
        )


class SkippingDiagnoser:
    def __init__(
        self,
        fault_category: str,
    ) -> None:
        self.fault_category = fault_category

    def diagnose(
        self,
        state: dict,
    ) -> DiagnosisCallResult:
        evidence_id = state["evidence"][0][
            "evidence_id"
        ]

        if self.fault_category == (
            "no_fault_detected"
        ):
            root_cause = (
                "现有证据未检测到故障。"
            )
        else:
            root_cause = (
                "现有证据不足以确定根因。"
            )

        return DiagnosisCallResult(
            diagnosis=Diagnosis(
                fault_category=(
                    self.fault_category
                ),
                root_cause=root_cause,
                evidence_ids=[evidence_id],
                runbook_ids=[],
                confidence=0.3,
                reasoning_summary=(
                    "现有Kubernetes证据不足。"
                ),
            ),
            model_name="fake-model",
            usage={},
        )

def invoke_graph(graph):
    return graph.invoke(
        {
            "request": {
                "namespace": "agent-demo",
                "service_name": "order-service",
                "description": "服务状态异常",
            }
        }
    )


def test_collector_only_graph_stops_after_collection():
    graph = build_incident_graph(
        collector=FakeCollector(
            result=healthy_bundle()
        )
    )

    result = invoke_graph(graph)

    assert result["phase"] == "evidence_collected"
    assert result["evidence"]


def test_collector_and_retriever_stop_after_retrieval():
    retriever = FakeRetriever(
        result=runbook_result()
    )

    graph = build_incident_graph(
        collector=FakeCollector(
            result=healthy_bundle()
        ),
        retriever=retriever,
    )

    result = invoke_graph(graph)

    assert result["phase"] == "runbooks_retrieved"
    assert len(result["retrieved_runbooks"]) == 1
    assert len(retriever.queries) == 1


def test_full_graph_completes_diagnosis():
    diagnoser = StateAwareDiagnoser()

    graph = build_incident_graph(
        collector=FakeCollector(
            result=healthy_bundle()
        ),
        retriever=FakeRetriever(
            result=runbook_result()
        ),
        diagnoser=diagnoser,
    )

    result = invoke_graph(graph)

    assert result["phase"] == "diagnosis_completed"
    assert result["errors"] == []
    assert result["llm_model"] == "fake-model"
    assert result["llm_usage"]["total_tokens"] == 130
    assert len(diagnoser.calls) == 1


def test_diagnoser_without_retriever_is_rejected():
    with pytest.raises(
        ValueError,
        match="diagnoser requires a runbook retriever",
    ):
        build_incident_graph(
            collector=FakeCollector(
                result=healthy_bundle()
            ),
            diagnoser=StateAwareDiagnoser(),
        )


def test_retrieval_error_stops_before_diagnosis():
    diagnoser = StateAwareDiagnoser()

    graph = build_incident_graph(
        collector=FakeCollector(
            result=healthy_bundle()
        ),
        retriever=FakeRetriever(
            error=TimeoutError(
                "vector store timed out"
            )
        ),
        diagnoser=diagnoser,
    )

    result = invoke_graph(graph)

    assert result["phase"] == "runbook_retrieval_failed"
    assert result["errors"][-1]["code"] == (
        "RETRIEVAL_ERROR"
    )
    assert diagnoser.calls == []

def test_empty_retrieval_stops_before_diagnosis():
    diagnoser = StateAwareDiagnoser()

    graph = build_incident_graph(
        collector=FakeCollector(
            result=healthy_bundle()
        ),
        retriever=FakeRetriever(result=[]),
        diagnoser=diagnoser,
    )

    result = invoke_graph(graph)

    assert result["phase"] == (
        "runbook_retrieval_failed"
    )
    assert result["errors"][-1]["code"] == (
        "NO_RUNBOOK_FOUND"
    )
    assert diagnoser.calls == []

def test_full_graph_completes_remediation_plan():
    planner = StateAwareRemediationPlanner()

    graph = build_incident_graph(
        collector=FakeCollector(
            result=healthy_bundle()
        ),
        retriever=FakeRetriever(
            result=runbook_result()
        ),
        diagnoser=StateAwareDiagnoser(),
        planner=planner,
    )

    result = invoke_graph(graph)

    assert result["phase"] == (
        "remediation_planned"
    )
    assert result["remediation_plan"][
        "action"
    ] == "manual_investigation"
    assert result["risk_level"] == "low"
    assert result["requires_approval"] is False
    assert len(planner.calls) == 1


@pytest.mark.parametrize(
    "fault_category",
    [
        "unknown",
        "no_fault_detected",
    ],
)
def test_non_actionable_diagnosis_skips_planner(
    fault_category,
):
    planner = FakeRemediationPlanner()

    graph = build_incident_graph(
        collector=FakeCollector(
            result=healthy_bundle()
        ),
        retriever=FakeRetriever(
            result=runbook_result()
        ),
        diagnoser=SkippingDiagnoser(
            fault_category
        ),
        planner=planner,
    )

    result = invoke_graph(graph)

    assert result["phase"] == (
        "remediation_skipped"
    )
    assert result["remediation_plan"] is None
    assert result["requires_approval"] is False
    assert planner.calls == []


def test_planner_without_diagnoser_is_rejected():
    with pytest.raises(
        ValueError,
        match="planner requires a diagnoser",
    ):
        build_incident_graph(
            collector=FakeCollector(
                result=healthy_bundle()
            ),
            retriever=FakeRetriever(
                result=runbook_result()
            ),
            planner=FakeRemediationPlanner(),
        )


def test_planner_error_is_written_to_graph_state():
    planner = FakeRemediationPlanner(
        error=TimeoutError(
            "remediation model timed out"
        )
    )

    graph = build_incident_graph(
        collector=FakeCollector(
            result=healthy_bundle()
        ),
        retriever=FakeRetriever(
            result=runbook_result()
        ),
        diagnoser=StateAwareDiagnoser(),
        planner=planner,
    )

    result = invoke_graph(graph)

    assert result["phase"] == (
        "remediation_failed"
    )
    assert result["errors"][-1]["code"] == (
        "LLM_REMEDIATION_ERROR"
    )    