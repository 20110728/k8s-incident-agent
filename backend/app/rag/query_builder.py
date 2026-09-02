import json
from typing import Any

from backend.app.agent.state import IncidentState


RETRIEVAL_EVIDENCE_TYPES = {
    "Service",
    "PodSelection",
    "EndpointSlice",
    "PodStatus",
    "PodEvents",
    "PodLogs",
    "Deployment",
    "Node",
}

MAX_ITEM_CHARACTERS = 1200
MAX_QUERY_CHARACTERS = 8000


def build_retrieval_query(
    state: IncidentState,
) -> str:
    request = state.get("request", {})
    description = request.get("description", "")

    parts = [
        f"用户描述：{description}",
        (
            "目标服务："
            f"{request.get('namespace', '')}/"
            f"{request.get('service_name', '')}"
        ),
    ]

    for item in state.get("evidence", []):
        resource_type = item.get("resource_type")

        if resource_type not in RETRIEVAL_EVIDENCE_TYPES:
            continue

        serialized = json.dumps(
            item.get("data", {}),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

        if len(serialized) > MAX_ITEM_CHARACTERS:
            serialized = (
                serialized[:MAX_ITEM_CHARACTERS]
                + "...[truncated]"
            )

        parts.append(
            (
                f"{resource_type} "
                f"{item.get('resource_name', '')}："
                f"{serialized}"
            )
        )

    query = "\n".join(parts)

    return query[:MAX_QUERY_CHARACTERS]