from backend.app.schemas.kubernetes import (
    EndpointInfo,
    EndpointSliceInfo,
    ServiceInfo,
    ServicePortInfo,
)
from backend.app.tools.client import (
    REQUEST_TIMEOUT,
    KubernetesClients,
)


def selector_to_string(
    selector: dict[str, str],
) -> str:
    """Convert a selector dictionary to Kubernetes label syntax."""
    return ",".join(f"{key}={value}" for key, value in sorted(selector.items()))


def get_service(
    clients: KubernetesClients,
    namespace: str,
    service_name: str,
) -> ServiceInfo:
    service = clients.core.read_namespaced_service(
        name=service_name,
        namespace=namespace,
        _request_timeout=REQUEST_TIMEOUT,
    )

    ports = [
        ServicePortInfo(
            name=port.name,
            port=port.port,
            target_port=port.target_port,
            protocol=port.protocol or "TCP",
        )
        for port in service.spec.ports or []
    ]

    return ServiceInfo(
        uid=getattr(service.metadata, "uid", None),
        resource_version=getattr(service.metadata, "resource_version", None),
        namespace=namespace,
        name=service.metadata.name,
        service_type=service.spec.type,
        cluster_ip=service.spec.cluster_ip,
        selector=service.spec.selector or {},
        ports=ports,
    )


def list_service_pod_names(
    clients: KubernetesClients,
    namespace: str,
    service_name: str,
) -> list[str]:
    service = get_service(
        clients=clients,
        namespace=namespace,
        service_name=service_name,
    )

    if not service.selector:
        return []

    pods = clients.core.list_namespaced_pod(
        namespace=namespace,
        label_selector=selector_to_string(service.selector),
        _request_timeout=REQUEST_TIMEOUT,
    )

    return sorted(pod.metadata.name for pod in pods.items)


def list_namespace_pod_names(
    clients: KubernetesClients,
    namespace: str,
) -> list[str]:
    pods = clients.core.list_namespaced_pod(
        namespace=namespace,
        _request_timeout=REQUEST_TIMEOUT,
    )

    return sorted(pod.metadata.name for pod in pods.items)


def get_service_endpoint_slices(
    clients: KubernetesClients,
    namespace: str,
    service_name: str,
) -> list[EndpointSliceInfo]:
    slices = clients.discovery.list_namespaced_endpoint_slice(
        namespace=namespace,
        label_selector=(f"kubernetes.io/service-name={service_name}"),
        _request_timeout=REQUEST_TIMEOUT,
    )

    result: list[EndpointSliceInfo] = []

    for endpoint_slice in slices.items:
        endpoints: list[EndpointInfo] = []

        for endpoint in endpoint_slice.endpoints or []:
            conditions = endpoint.conditions
            target_ref = endpoint.target_ref

            endpoints.append(
                EndpointInfo(
                    target_uid=getattr(target_ref, "uid", None),
                    target_namespace=getattr(target_ref, "namespace", None),
                    addresses=endpoint.addresses or [],
                    ready=(conditions.ready if conditions is not None else None),
                    serving=(conditions.serving if conditions is not None else None),
                    terminating=(
                        conditions.terminating if conditions is not None else None
                    ),
                    node_name=endpoint.node_name,
                    target_kind=(target_ref.kind if target_ref is not None else None),
                    target_name=(target_ref.name if target_ref is not None else None),
                )
            )

        result.append(
            EndpointSliceInfo(
                uid=getattr(endpoint_slice.metadata, "uid", None),
                resource_version=getattr(endpoint_slice.metadata, "resource_version", None),
                service_uid=next((owner.uid for owner in (getattr(endpoint_slice.metadata, "owner_references", None) or [])
                                  if owner.kind == "Service" and owner.name == service_name), None),
                namespace=namespace,
                name=endpoint_slice.metadata.name,
                service_name=service_name,
                address_type=endpoint_slice.address_type,
                endpoints=endpoints,
            )
        )

    return result
