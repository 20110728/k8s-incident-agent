import json
from types import SimpleNamespace

import pytest

from backend.app.agent.schemas import Diagnosis
from backend.app.llm.context_builder import (
    build_diagnosis_context,
)
from backend.app.llm.diagnoser import (
    ChatDiagnosisService,
)


def diagnosis_state() -> dict:
    return {
        "request": {
            "namespace": "agent-demo",
            "service_name": "order-service",
            "description": "Pod无法Ready",
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
                "runbook_id": "wrong-http-path",
                "category": "readiness",
                "title": "探针路径错误",
                "section": "判断规则",
                "content": "检查Readiness Probe路径。",
            }
        ],
    }


def valid_diagnosis() -> Diagnosis:
    return Diagnosis(
        fault_category="readiness_probe_error",
        root_cause="Readiness Probe路径错误",
        evidence_ids=["ev-test-001"],
        runbook_ids=["wrong-http-path"],
        confidence=0.9,
        reasoning_summary="Pod未Ready。",
    )


def test_context_contains_available_ids():
    context = build_diagnosis_context(
        diagnosis_state()
    )
    payload = json.loads(context)

    assert payload["available_evidence_ids"] == [
        "ev-test-001"
    ]
    assert payload["available_runbook_ids"] == [
        "wrong-http-path"
    ]
    assert payload["incident"]["namespace"] == (
        "agent-demo"
    )


class FakeStructuredModel:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.messages = None

    def invoke(self, messages):
        self.messages = messages
        return self.response


class FakeChatModel:
    def __init__(self, response: dict) -> None:
        self.structured = FakeStructuredModel(
            response
        )
        self.schema = None
        self.options = None

    def with_structured_output(
        self,
        schema,
        **options,
    ):
        self.schema = schema
        self.options = options
        return self.structured


def test_chat_diagnoser_returns_parsed_result():
    raw = SimpleNamespace(
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 25,
            "total_tokens": 125,
        }
    )

    model = FakeChatModel(
        {
            "parsed": valid_diagnosis(),
            "parsing_error": None,
            "raw": raw,
        }
    )

    service = ChatDiagnosisService(
        model=model,
        model_name="fake-model",
    )

    result = service.diagnose(
        diagnosis_state()
    )

    assert result.diagnosis == valid_diagnosis()
    assert result.model_name == "fake-model"
    assert result.usage["total_tokens"] == 125
    assert model.schema is Diagnosis
    assert model.options["strict"] is True
    assert model.options["include_raw"] is True


def test_chat_diagnoser_rejects_parsing_error():
    model = FakeChatModel(
        {
            "parsed": None,
            "parsing_error": ValueError(
                "invalid structured output"
            ),
            "raw": None,
        }
    )

    service = ChatDiagnosisService(
        model=model,
        model_name="fake-model",
    )

    with pytest.raises(
        ValueError,
        match="structured diagnosis parsing failed",
    ):
        service.diagnose(
            diagnosis_state()
        )


def test_chat_diagnoser_rejects_missing_result():
    model = FakeChatModel(
        {
            "parsed": None,
            "parsing_error": None,
            "raw": None,
        }
    )

    service = ChatDiagnosisService(
        model=model,
        model_name="fake-model",
    )

    with pytest.raises(
        ValueError,
        match="model returned no parsed diagnosis",
    ):
        service.diagnose(
            diagnosis_state()
        )

def test_context_redacts_secret_without_breaking_data_json():
    state = diagnosis_state()

    state["evidence"][0]["data"] = {
        "message": "token=super-secret",
    }

    state["retrieved_runbooks"][0][
        "content"
    ] = "password=runbook-secret"

    context = build_diagnosis_context(state)
    payload = json.loads(context)

    assert "super-secret" not in context
    assert "runbook-secret" not in context
    assert "[REDACTED]" in context

    serialized_data = payload["evidence"][0]["data"]

    # Evidence data本身也必须仍然是有效JSON。
    parsed_data = json.loads(serialized_data)

    assert parsed_data["message"] == (
        "token=[REDACTED]"
    )