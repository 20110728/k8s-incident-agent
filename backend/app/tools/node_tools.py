from typing import Any

from backend.app.schemas.kubernetes import (
    NodeConditionInfo,
    NodeInfo,
)
from backend.app.tools.client import (
    REQUEST_TIMEOUT,
    KubernetesClients,
)


def _resource_values(
    values: dict[str, Any] | None,
) -> dict[str, str]:
    return {str(key): str(value) for key, value in (values or {}).items()}


def get_node_status(
    clients: KubernetesClients,
    node_name: str,
) -> NodeInfo:
    node = clients.core.read_node(
        name=node_name,
        _request_timeout=REQUEST_TIMEOUT,
    )

    conditions = [
        NodeConditionInfo(
            condition_type=condition.type,
            status=condition.status,
            reason=condition.reason,
            message=condition.message,
        )
        for condition in node.status.conditions or []
    ]

    ready = any(
        condition.condition_type == "Ready" and condition.status == "True"
        for condition in conditions
    )

    return NodeInfo(
        name=node.metadata.name,
        ready=ready,
        capacity=_resource_values(node.status.capacity),
        allocatable=_resource_values(node.status.allocatable),
        conditions=conditions,
    )
