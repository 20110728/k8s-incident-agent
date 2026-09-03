from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
)
from langchain_openai import ChatOpenAI

from backend.app.agent.schemas import (
    RemediationPlan,
)
from backend.app.llm.prompts import (
    REMEDIATION_SYSTEM_PROMPT,
    REMEDIATION_USER_TEMPLATE,
)
from backend.app.llm.remediation_context import (
    build_remediation_context,
)


@dataclass(frozen=True)
class RemediationCallResult:
    plan: RemediationPlan
    model_name: str
    usage: dict[str, int]


class RemediationPlannerPort(Protocol):
    def plan(
        self,
        state: dict[str, Any],
    ) -> RemediationCallResult:
        ...


class ChatRemediationPlanner:
    def __init__(
        self,
        model: ChatOpenAI,
        model_name: str,
    ) -> None:
        self._model_name = model_name
        self._structured_model = (
            model.with_structured_output(
                RemediationPlan,
                method="json_schema",
                strict=True,
                include_raw=True,
            )
        )

    def plan(
        self,
        state: dict[str, Any],
    ) -> RemediationCallResult:
        context = build_remediation_context(
            state
        )

        response = self._structured_model.invoke(
            [
                SystemMessage(
                    content=(
                        REMEDIATION_SYSTEM_PROMPT
                    )
                ),
                HumanMessage(
                    content=(
                        REMEDIATION_USER_TEMPLATE.format(
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
                "structured remediation parsing failed"
            ) from parsing_error

        parsed = response.get("parsed")

        if parsed is None:
            raise ValueError(
                "model returned no parsed "
                "remediation plan"
            )

        if not isinstance(
            parsed,
            RemediationPlan,
        ):
            parsed = RemediationPlan.model_validate(
                parsed
            )

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
                    for key, value in (
                        raw_usage.items()
                    )
                    if isinstance(value, int)
                }

        return RemediationCallResult(
            plan=parsed,
            model_name=self._model_name,
            usage=usage,
        )