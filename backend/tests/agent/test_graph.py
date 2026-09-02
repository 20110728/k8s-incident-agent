from backend.app.agent.graph import build_incident_graph
from backend.app.agent.nodes import make_diagnose_incident_node
from backend.app.agent.schemas import Diagnosis
from backend.tests.agent.fakes import (
    FakeCollector,
    FakeDiagnoser,
)


def test_valid_request_collects_evidence():
    collector = FakeCollector(
        result={
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
    )

    graph = build_incident_graph(collector)

    result = graph.invoke(
        {
            "request": {
                "namespace": "agent-demo",
                "service_name": "order-service",
                "description": "服务无法访问",
            }
        }
    )

    assert result["phase"] == "evidence_collected"
    assert len(result["evidence"]) == 4

    resource_types = {
        item["resource_type"]
        for item in result["evidence"]
    }

    assert resource_types == {
        "Service",
        "PodSelection",
        "PodStatus",
        "PodEvents",
    }


def test_collector_failure_is_written_to_state():
    collector = FakeCollector(error=TimeoutError("kubernetes request timed out"))

    graph = build_incident_graph(collector)

    result = graph.invoke(
        {
            "request": {
                "namespace": "agent-demo",
                "service_name": "order-service",
                "description": "服务无法访问",
            }
        }
    )

    assert result["phase"] == "evidence_collection_failed"
    assert result["errors"][-1]["code"] == "COLLECTION_ERROR"


def test_empty_evidence_is_a_failure():
    empty_bundle = {
        "namespace": "agent-demo",
        "service_name": "order-service",
        "service": None,
        "service_pod_names": [],
        "namespace_pod_names": [],
        "endpoint_slices": [],
        "pod_statuses": {},
        "pod_events": {},
        "pod_logs": [],
        "owner_chains": {},
        "deployments": {},
        "nodes": {},
        "errors": [],
    }

    graph = build_incident_graph(
        FakeCollector(result=empty_bundle)
    )

    result = graph.invoke(
        {
            "request": {
                "namespace": "agent-demo",
                "service_name": "order-service",
                "description": "服务无法访问",
            }
        }
    )

    assert result["phase"] == "evidence_collection_failed"
    assert result["errors"][-1]["code"] == "EMPTY_EVIDENCE"
    assert result.get("evidence", []) == []

def test_malformed_bundle_is_a_collection_error():
    graph = build_incident_graph(
        FakeCollector(result={})
    )

    result = graph.invoke(
        {
            "request": {
                "namespace": "agent-demo",
                "service_name": "order-service",
                "description": "服务无法访问",
            }
        }
    )

    assert result["phase"] == "evidence_collection_failed"
    assert result["errors"][-1]["code"] == "COLLECTION_ERROR"
    assert "namespace" in result["errors"][-1]["message"]

def test_valid_diagnosis_is_written_to_state():
    diagnoser = FakeDiagnoser(
        diagnosis=Diagnosis(
            fault_category="readiness_probe_error",
            root_cause="Readiness Probe路径错误",
            evidence_ids=["ev-test-001"],
            runbook_ids=["wrong-http-path"],
            confidence=0.92,
            reasoning_summary=(
                "Pod处于Running但未Ready，"
                "事件显示探针返回404。"
            ),
        )
    )

    state = {
        "evidence": [
            {
                "evidence_id": "ev-test-001",
            }
        ],
        "retrieved_runbooks": [
            {
                "runbook_id": "wrong-http-path",
            }
        ],
    }

    node = make_diagnose_incident_node(
        diagnoser
    )
    result = node(state)

    assert result["phase"] == "diagnosis_completed"
    assert (
        result["diagnosis"]["fault_category"]
        == "readiness_probe_error"
    )
    assert result["llm_model"] == "fake-model"

def test_unknown_evidence_reference_is_rejected():
    diagnoser = FakeDiagnoser(
        diagnosis=Diagnosis(
            fault_category="readiness_probe_error",
            root_cause="探针路径错误",
            evidence_ids=["ev-does-not-exist"],
            runbook_ids=["wrong-http-path"],
            confidence=0.8,
            reasoning_summary="探针返回404。",
        )
    )

    node = make_diagnose_incident_node(
        diagnoser
    )

    result = node(
        {
            "evidence": [
                {
                    "evidence_id": "ev-real-001",
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
    )

    assert result["phase"] == "diagnosis_failed"
    assert (
        result["errors"][-1]["code"]
        == "INVALID_DIAGNOSIS_REFERENCE"
    )

