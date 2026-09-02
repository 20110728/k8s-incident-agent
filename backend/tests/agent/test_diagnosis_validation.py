from backend.app.agent.nodes import (
    make_diagnose_incident_node,
)
from backend.app.agent.schemas import Diagnosis
from backend.tests.agent.fakes import FakeDiagnoser


def diagnosis_state() -> dict:
    return {
        "evidence": [
            {
                "evidence_id": "ev-real-001",
            },
            {
                "evidence_id": "ev-real-002",
            },
        ],
        "retrieved_runbooks": [
            {
                "runbook_id": "wrong-http-path",
            }
        ],
        "errors": [],
        "error_count": 0,
    }


def test_unknown_runbook_reference_is_rejected():
    diagnoser = FakeDiagnoser(
        diagnosis=Diagnosis(
            fault_category="readiness_probe_error",
            root_cause="探针路径错误",
            evidence_ids=["ev-real-001"],
            runbook_ids=["not-found-runbook"],
            confidence=0.8,
            reasoning_summary="探针检查失败。",
        )
    )

    result = make_diagnose_incident_node(
        diagnoser
    )(diagnosis_state())

    assert result["phase"] == "diagnosis_failed"
    assert result["errors"][-1]["code"] == (
        "INVALID_DIAGNOSIS_REFERENCE"
    )
    assert "unknown runbook IDs" in (
        result["errors"][-1]["message"]
    )


def test_fault_diagnosis_requires_runbook():
    diagnoser = FakeDiagnoser(
        diagnosis=Diagnosis(
            fault_category="readiness_probe_error",
            root_cause="探针路径错误",
            evidence_ids=["ev-real-001"],
            runbook_ids=[],
            confidence=0.8,
            reasoning_summary="探针检查失败。",
        )
    )

    result = make_diagnose_incident_node(
        diagnoser
    )(diagnosis_state())

    assert result["phase"] == "diagnosis_failed"
    assert result["errors"][-1]["code"] == (
        "INVALID_DIAGNOSIS_REFERENCE"
    )
    assert "at least one retrieved runbook" in (
        result["errors"][-1]["message"]
    )


def test_unknown_diagnosis_allows_empty_runbooks():
    diagnoser = FakeDiagnoser(
        diagnosis=Diagnosis(
            fault_category="unknown",
            root_cause="现有证据不足以确定根因",
            evidence_ids=["ev-real-001"],
            runbook_ids=[],
            confidence=0.3,
            reasoning_summary=(
                "Kubernetes资源正常，但缺少HTTP检查。"
            ),
        )
    )

    result = make_diagnose_incident_node(
        diagnoser
    )(diagnosis_state())

    assert result["phase"] == "diagnosis_completed"
    assert result["diagnosis"]["runbook_ids"] == []


def test_diagnoser_exception_is_recorded():
    diagnoser = FakeDiagnoser(
        error=TimeoutError("LLM timed out")
    )

    result = make_diagnose_incident_node(
        diagnoser
    )(diagnosis_state())

    assert result["phase"] == "diagnosis_failed"
    assert result["errors"][-1]["code"] == (
        "LLM_DIAGNOSIS_ERROR"
    )
    assert "LLM timed out" in (
        result["errors"][-1]["message"]
    )

def test_reference_edge_punctuation_is_normalized():
    diagnoser = FakeDiagnoser(
        diagnosis=Diagnosis(
            fault_category="readiness_probe_error",
            root_cause="Readiness Probe路径错误",
            evidence_ids=[
                " ,ev-real-001， ",
            ],
            runbook_ids=[
                " ;wrong-http-path； ",
            ],
            confidence=0.9,
            reasoning_summary=(
                "Pod状态与Runbook支持该诊断。"
            ),
        )
    )

    result = make_diagnose_incident_node(
        diagnoser
    )(diagnosis_state())

    assert result["phase"] == "diagnosis_completed"
    assert result["diagnosis"]["evidence_ids"] == [
        "ev-real-001"
    ]
    assert result["diagnosis"]["runbook_ids"] == [
        "wrong-http-path"
    ]


def test_mentioned_but_uncited_evidence_is_rejected():
    diagnoser = FakeDiagnoser(
        diagnosis=Diagnosis(
            fault_category="readiness_probe_error",
            root_cause="Readiness Probe路径错误",
            evidence_ids=[
                "ev-real-001",
            ],
            runbook_ids=[
                "wrong-http-path",
            ],
            confidence=0.9,
            reasoning_summary=(
                "证据ev-real-002显示探针返回404，"
                "因此判断探针路径错误。"
            ),
        )
    )

    result = make_diagnose_incident_node(
        diagnoser
    )(diagnosis_state())

    assert result["phase"] == "diagnosis_failed"
    assert result["errors"][-1]["code"] == (
        "INVALID_DIAGNOSIS_REFERENCE"
    )
    assert "missing from evidence_ids" in (
        result["errors"][-1]["message"]
    )