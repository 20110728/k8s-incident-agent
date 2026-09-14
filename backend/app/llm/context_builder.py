from backend.app.agent.diagnosis_policy import diagnostic_facts

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

    # Put business results before potentially long event/log text.
    ordered_evidence = sorted(evidence, key=lambda item: item.get("resource_type") != "BusinessCheck")
    for item in ordered_evidence:
        resource_type = item.get(
            "resource_type",
            "Unknown",
        )

        limit = (
            MAX_LOG_CHARACTERS
            if resource_type == "PodLogs"
            else MAX_EVIDENCE_CHARACTERS
        )

        if resource_type == "BusinessCheck":
            limit = 4000

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

    facts = diagnostic_facts(state)

    context = {
        "output_contract": {
            "always_required_evidence_ids": facts["business_evidence_ids"],
            "configuration_categories": [
                "readiness_probe_error",
                "service_selector_mismatch",
            ],
            "configuration_required_evidence_ids": (
                facts["configuration_evidence_ids"]
            ),
            "instructions": [
                (
                    "若选择配置故障类别，顶层 evidence_ids 必须包含 "
                    "configuration_required_evidence_ids 中全部 ID；"
                    "仅引用 Deployment 不够。"
                ),
                (
                    "顶层 evidence_ids 必须包含 always_required_evidence_ids，"
                    "以及所有结构化症状和假设使用的 ID。"
                ),
                (
                    "以上引用要求不证明故障成立；仍需依据 policy_facts、"
                    "实际证据及匹配的 service_profile 判断。"
                ),
                (
                    "有故障或 unknown 时填写真实的 missing_evidence 与 "
                    "next_investigation；区分配置漂移已确认与变更来源尚未确认，"
                    "不编造缺失依赖。"
                ),
                (
                    "仅出现 ready=false 的端点仍可能保留在 EndpointSlice 中，"
                    "不表述为已被删除。未取得处理请求的副本身份时，"
                    "不确定声称由哪个副本响应。"
                ),
            ],
        },
        "previous_validation_feedback": (
            redact_sensitive_text(str(validation_feedback))
            if validation_feedback
            else None
        ),
        "policy_facts": facts,
        "service_profile": state.get("service_profile"),
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