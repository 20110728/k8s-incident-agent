import json
from typing import Any

from backend.app.agent.remediation_policy import (
    get_allowed_remediation_actions,
)

from backend.app.agent.state import IncidentState
from backend.app.llm.context_builder import (
    redact_sensitive_text,
    serialize_limited,
)


MAX_REMEDIATION_EVIDENCE_CHARACTERS = 1400
MAX_REMEDIATION_RUNBOOK_CHARACTERS = 1600
MAX_REMEDIATION_CONTEXT_CHARACTERS = 24000


def _redact_json_value(
    value: Any,
) -> Any:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )

    redacted = redact_sensitive_text(serialized)

    return json.loads(redacted)


def build_remediation_context(
    state: IncidentState,
) -> str:
    request = state.get("request", {})
    diagnosis = state.get("diagnosis") or {}

    fault_category = diagnosis.get(
        "fault_category"
    )

    diagnosis_evidence_ids = set(
        diagnosis.get("evidence_ids", [])
    )
    diagnosis_runbook_ids = set(
        diagnosis.get("runbook_ids", [])
    )

    evidence_blocks: list[dict[str, Any]] = []

    for item in state.get("evidence", []):
        evidence_id = item.get("evidence_id")

        if evidence_id not in diagnosis_evidence_ids:
            continue

        evidence_blocks.append(
            {
                "evidence_id": evidence_id,
                "resource_type": item.get(
                    "resource_type"
                ),
                "resource_name": item.get(
                    "resource_name"
                ),
                "data": serialize_limited(
                    item.get("data", {}),
                    MAX_REMEDIATION_EVIDENCE_CHARACTERS,
                ),
            }
        )

    runbook_blocks: list[dict[str, Any]] = []

    for item in state.get(
        "retrieved_runbooks",
        [],
    ):
        runbook_id = item.get("runbook_id")

        if runbook_id not in diagnosis_runbook_ids:
            continue

        runbook_blocks.append(
            {
                "runbook_id": runbook_id,
                "category": item.get("category"),
                "title": item.get("title"),
                "section": item.get("section"),
                "content": redact_sensitive_text(
                    str(item.get("content", ""))
                )[
                    :MAX_REMEDIATION_RUNBOOK_CHARACTERS
                ],
            }
        )

    allowed_actions = sorted(
        get_allowed_remediation_actions(state)
    )

    context = {
        "incident": {
            "namespace": request.get("namespace"),
            "service_name": request.get(
                "service_name"
            ),
            "user_description": (
                redact_sensitive_text(
                    str(
                        request.get(
                            "description",
                            "",
                        )
                    )
                )
            ),
        },
        "diagnosis": _redact_json_value(
            diagnosis
        ),
        "allowed_actions": allowed_actions,
        "available_evidence_ids": [
            item["evidence_id"]
            for item in evidence_blocks
        ],
        "available_runbook_ids": sorted(
            {
                item["runbook_id"]
                for item in runbook_blocks
            }
        ),
        "evidence": evidence_blocks,
        "runbooks": runbook_blocks,
        "safety_constraints": {
            "allowed_namespace": "agent-demo",
            "execution_enabled": False,
            "shell_commands_allowed": False,
            "approval_required_for_execution": True,
        },
    }

    serialized = json.dumps(
        context,
        ensure_ascii=False,
        indent=2,
        default=str,
    )

    if (
        len(serialized)
        > MAX_REMEDIATION_CONTEXT_CHARACTERS
    ):
        raise ValueError(
            "remediation context exceeds "
            "maximum allowed size"
        )

    return serialized