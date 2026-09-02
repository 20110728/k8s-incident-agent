from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol


class EvidenceCollectorPort(Protocol):
    def collect(
        self,
        namespace: str,
        service_name: str,
    ) -> dict[str, Any]:
        ...


class KubernetesCollectorAdapter:
    def __init__(self, collect_fn) -> None:
        self._collect_fn = collect_fn

    def collect(
        self,
        namespace: str,
        service_name: str,
    ) -> dict[str, Any]:
        result = self._collect_fn(
            namespace,
            service_name,
        )

        if hasattr(result, "model_dump"):
            result = result.model_dump()

        if not isinstance(result, dict):
            raise TypeError(
                "collect_service_evidence must return "
                "a dict or Pydantic model"
            )

        return result

def normalize_evidence(
    *,
    incident_id: str,
    bundle: dict[str, Any],
) -> list[dict[str, Any]]:
    collected_at = datetime.now(UTC).isoformat()
    short_id = incident_id.split("-")[0]
    evidence: list[dict[str, Any]] = []
    sequence = 0

    def add_evidence(
        *,
        resource_type: str,
        resource_name: str,
        data: Any,
    ) -> None:
        nonlocal sequence
        sequence += 1

        if not isinstance(data, dict):
            data = {"value": data}

        evidence.append(
            {
                "evidence_id": (
                    f"ev-{short_id}-{sequence:03d}"
                ),
                "source": "kubernetes_api",
                "resource_type": resource_type,
                "resource_name": resource_name,
                "collected_at": collected_at,
                "data": data,
                "error": None,
            }
        )

    namespace = str(bundle["namespace"])
    service_name = str(bundle["service_name"])

    # 1. Service
    service = bundle.get("service")
    if service:
        add_evidence(
            resource_type="Service",
            resource_name=service_name,
            data=service,
        )

    # 2. Service选择Pod的结果
    # selector mismatch诊断必须同时比较这两个列表。
    service_pod_names = bundle.get(
        "service_pod_names",
        [],
    )
    namespace_pod_names = bundle.get(
        "namespace_pod_names",
        [],
    )

    # Service存在，或者任一Pod列表有内容时，
    # Pod选择关系才构成一条有效证据。
    if service or service_pod_names or namespace_pod_names:
        add_evidence(
            resource_type="PodSelection",
            resource_name=service_name,
            data={
                "namespace": namespace,
                "service_pod_names": service_pod_names,
                "namespace_pod_names": namespace_pod_names,
            },
        )

    # 3. EndpointSlice
    for index, endpoint_slice in enumerate(
        bundle.get("endpoint_slices", []),
        start=1,
    ):
        if isinstance(endpoint_slice, dict):
            resource_name = endpoint_slice.get(
                "name",
                f"{service_name}-endpointslice-{index}",
            )
        else:
            resource_name = (
                f"{service_name}-endpointslice-{index}"
            )

        add_evidence(
            resource_type="EndpointSlice",
            resource_name=str(resource_name),
            data=endpoint_slice,
        )

    # 4. Pod状态
    for pod_name, pod_status in bundle.get(
        "pod_statuses",
        {},
    ).items():
        add_evidence(
            resource_type="PodStatus",
            resource_name=pod_name,
            data=pod_status,
        )

    # 5. Pod事件
    for pod_name, pod_events in bundle.get(
        "pod_events",
        {},
    ).items():
        add_evidence(
            resource_type="PodEvents",
            resource_name=pod_name,
            data={"events": pod_events},
        )

    # 6. Pod日志
    for index, pod_log in enumerate(
        bundle.get("pod_logs", []),
        start=1,
    ):
        if isinstance(pod_log, dict):
            pod_name = (
                pod_log.get("pod_name")
                or pod_log.get("name")
                or f"unknown-pod-{index}"
            )
        else:
            pod_name = f"unknown-pod-{index}"

        add_evidence(
            resource_type="PodLogs",
            resource_name=str(pod_name),
            data=pod_log,
        )

    # 7. Pod → ReplicaSet → Deployment归属链
    for pod_name, owner_chain in bundle.get(
        "owner_chains",
        {},
    ).items():
        add_evidence(
            resource_type="OwnerChain",
            resource_name=pod_name,
            data={"owner_chain": owner_chain},
        )

    # 8. Deployment配置
    for deployment_name, deployment in bundle.get(
        "deployments",
        {},
    ).items():
        add_evidence(
            resource_type="Deployment",
            resource_name=deployment_name,
            data=deployment,
        )

    # 9. Node状态
    for node_name, node in bundle.get(
        "nodes",
        {},
    ).items():
        add_evidence(
            resource_type="Node",
            resource_name=node_name,
            data=node,
        )

    return evidence
