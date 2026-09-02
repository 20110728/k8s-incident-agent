from typing import Any

from backend.app.agent.schemas import Diagnosis
from backend.app.llm.diagnoser import (
    DiagnosisCallResult,
)

class FakeCollector:
    def __init__(
        self,
        result: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or {}
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def collect(
        self,
        namespace: str,
        service_name: str,
    ) -> dict[str, Any]:
        self.calls.append((namespace, service_name))

        if self.error is not None:
            raise self.error

        return self.result

class FakeRetriever:
    def __init__(
        self,
        result=None,
        error: Exception | None = None,
    ):
        self.result = result or []
        self.error = error
        self.queries: list[str] = []

    def retrieve(
        self,
        query: str,
        k: int = 3,
    ):
        self.queries.append(query)

        if self.error is not None:
            raise self.error

        return self.result[:k]

class FakeDiagnoser:
    def __init__(
        self,
        diagnosis: Diagnosis | None = None,
        error: Exception | None = None,
    ) -> None:
        self.diagnosis = diagnosis
        self.error = error
        self.calls: list[dict] = []

    def diagnose(
        self,
        state: dict,
    ) -> DiagnosisCallResult:
        self.calls.append(state)

        if self.error is not None:
            raise self.error

        if self.diagnosis is None:
            raise ValueError(
                "fake diagnosis was not configured"
            )

        return DiagnosisCallResult(
            diagnosis=self.diagnosis,
            model_name="fake-model",
            usage={
                "input_tokens": 100,
                "output_tokens": 30,
                "total_tokens": 130,
            },
        )