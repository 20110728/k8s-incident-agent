from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
)
from langchain_openai import ChatOpenAI

from backend.app.agent.schemas import Diagnosis
from backend.app.llm.context_builder import (
    build_diagnosis_context,
)
from backend.app.llm.prompts import (
    DIAGNOSIS_SYSTEM_PROMPT,
    DIAGNOSIS_USER_TEMPLATE,
)


@dataclass(frozen=True)
class DiagnosisCallResult:
    diagnosis: Diagnosis
    model_name: str
    usage: dict[str, int]


class DiagnosisServicePort(Protocol):
    def diagnose(
        self,
        state: dict[str, Any],
    ) -> DiagnosisCallResult:
        ...


class ChatDiagnosisService:
    def __init__(
        self,
        model: ChatOpenAI,
        model_name: str,
    ) -> None:
        self._model_name = model_name
        self._structured_model = (
            model.with_structured_output(
                Diagnosis,
                method="json_schema",
                strict=True,
                include_raw=True,
            )
        )

    def diagnose(
        self,
        state: dict[str, Any],
    ) -> DiagnosisCallResult:
        context = build_diagnosis_context(state)

        response = self._structured_model.invoke(
            [
                SystemMessage(
                    content=DIAGNOSIS_SYSTEM_PROMPT
                ),
                HumanMessage(
                    content=(
                        DIAGNOSIS_USER_TEMPLATE.format(
                            context=context
                        )
                    )
                ),
            ]
        )

        parsing_error = response.get(
            "parsing_error"
        )

        if parsing_error is not None:
            raise ValueError(
                "structured diagnosis parsing failed"
            ) from parsing_error

        parsed = response.get("parsed")

        if parsed is None:
            raise ValueError(
                "model returned no parsed diagnosis"
            )

        if not isinstance(parsed, Diagnosis):
            parsed = Diagnosis.model_validate(parsed)

        raw = response.get("raw")
        usage: dict[str, int] = {}

        if raw is not None:
            raw_usage = getattr(
                raw,
                "usage_metadata",
                None,
            )

            if isinstance(raw_usage, dict):
                usage = {
                    str(key): int(value)
                    for key, value in raw_usage.items()
                    if isinstance(value, int)
                }

        return DiagnosisCallResult(
            diagnosis=parsed,
            model_name=self._model_name,
            usage=usage,
        )