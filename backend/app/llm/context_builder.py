import json
import re
from typing import Any

from backend.app.agent.state import IncidentState


MAX_EVIDENCE_CHARACTERS = 1800
MAX_LOG_CHARACTERS = 3000
MAX_RUNBOOK_CHARACTERS = 2200
MAX_TOTAL_CONTEXT_CHARACTERS = 24000


SENSITIVE_PATTERN = re.compile(
    (
        r"(?i)"
        r"(authorization|api[_-]?key|token|password)"
        r"(\s*[:=]\s*)"
        r"([^\s,;\"'}\]]+)"
    )
)


def redact_sensitive_text(text: str) -> str:
    return SENSITIVE_PATTERN.sub(
        r"\1\2[REDACTED]",
        text,
    )


def serialize_limited(
    value: Any,
    limit: int,
) -> str:
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )

    text = redact_sensitive_text(text)

    if len(text) <= limit:
        return text

    return text[:limit] + "...[truncated]"



def build_diagnosis_context(
    state: IncidentState,
) -> str:
    request = state.get("request", {})
    evidence = state.get("evidence", [])
    runbooks = state.get("retrieved_runbooks", [])

    validation_feedback = state.get(
        "diagnosis_validation_feedback"
    )

    evidence_blocks: list[dict[str, Any]] = []

    for item in evidence:
        resource_type = item.get(
            "resource_type",
            "Unknown",
        )

        limit = (
            MAX_LOG_CHARACTERS
            if resource_type == "PodLogs"
            else MAX_EVIDENCE_CHARACTERS
        )

        evidence_blocks.append(
            {
                "evidence_id": item.get(
                    "evidence_id"
                ),
                "resource_type": resource_type,
                "resource_name": item.get(
                    "resource_name"
                ),
                "data": serialize_limited(
                    item.get("data", {}),
                    limit,
                ),
            }
        )

    runbook_blocks: list[dict[str, Any]] = []

    for item in runbooks:
        runbook_blocks.append(
            {
                "runbook_id": item.get("runbook_id"),
                "category": item.get("category"),
                "title": item.get("title"),
                "section": item.get("section"),
                "content": redact_sensitive_text(
                    str(item.get("content", ""))
                )[:MAX_RUNBOOK_CHARACTERS],
            }
        )

    context = {
        "incident": {
            "namespace": request.get("namespace"),
            "service_name": request.get(
                "service_name"
            ),
            "user_description": request.get(
                "description"
            ),
        },
        "available_evidence_ids": [
            item.get("evidence_id")
            for item in evidence
            if item.get("evidence_id")
        ],
        "available_runbook_ids": sorted(
            {
                item.get("runbook_id")
                for item in runbooks
                if item.get("runbook_id")
            }
        ),
        "evidence": evidence_blocks,
        "runbooks": runbook_blocks,
    }

    if validation_feedback:
        context[
            "previous_validation_feedback"
        ] = redact_sensitive_text(
            str(validation_feedback)
        )

    serialized = json.dumps(
        context,
        ensure_ascii=False,
        indent=2,
    )

    return serialized[
        :MAX_TOTAL_CONTEXT_CHARACTERS
    ]