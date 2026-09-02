import pytest

from backend.app.agent.graph import build_incident_graph
from backend.app.agent.schemas import Diagnosis
from backend.app.llm.diagnoser import DiagnosisCallResult
from backend.tests.agent.fakes import (
    FakeCollector,
    FakeRetriever,
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