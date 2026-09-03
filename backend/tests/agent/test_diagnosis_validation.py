from backend.app.agent.nodes import (
    make_diagnose_incident_node,
)

from backend.app.llm.diagnoser import (
    DiagnosisCallResult,
)

from backend.app.agent.schemas import Diagnosis
from backend.tests.agent.fakes import FakeDiagnoser

class SequenceDiagnoser:
    def __init__(
        self,
        diagnoses: list[Diagnosis],
    ) -> None:
        self.diagnoses = diagnoses
        self.calls: list[dict] = []

    def diagnose(
        self,
        state: dict,
    ) -> DiagnosisCallResult:
        diagnosis = self.diagnoses[
            len(self.calls)
        ]

        self.calls.append(state)

        return DiagnosisCallResult(
            diagnosis=diagnosis,
            model_name="fake-model",
            usage={
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        )

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
    assert len(diagnoser.calls) == 2
    assert result["diagnosis_retry_count"] == 1

def test_invalid_reference_succeeds_after_feedback_retry():
    first_diagnosis = Diagnosis(
        fault_category="image_pull_backoff",
        root_cause="容器镜像无法拉取",
        evidence_ids=[
            "ev-real-001",
        ],
        runbook_ids=[
            "wrong-http-path",
        ],
        confidence=0.9,
        reasoning_summary=(
            "证据ev-real-002显示镜像拉取失败。"
        ),
    )

    corrected_diagnosis = Diagnosis(
        fault_category="image_pull_backoff",
        root_cause="容器镜像无法拉取",
        evidence_ids=[
            "ev-real-001",
            "ev-real-002",
        ],
        runbook_ids=[
            "wrong-http-path",
        ],
        confidence=0.9,
        reasoning_summary=(
            "证据ev-real-002显示镜像拉取失败。"
        ),
    )

    diagnoser = SequenceDiagnoser(
        [
            first_diagnosis,
            corrected_diagnosis,
        ]
    )

    result = make_diagnose_incident_node(
        diagnoser
    )(diagnosis_state())

    assert result["phase"] == (
        "diagnosis_completed"
    )
    assert result["diagnosis"][
        "evidence_ids"
    ] == [
        "ev-real-001",
        "ev-real-002",
    ]

    assert result["diagnosis_retry_count"] == 1
    assert len(diagnoser.calls) == 2

    retry_state = diagnoser.calls[1]

    assert (
        "diagnosis_validation_feedback"
        in retry_state
    )
    assert "ev-real-002" in (
        retry_state[
            "diagnosis_validation_feedback"
        ]
    )

    assert result["llm_usage"][
        "total_tokens"
    ] == 30