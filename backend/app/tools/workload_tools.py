from typing import Any

from backend.app.schemas.kubernetes import (
    ContainerConfigInfo,
    DeploymentInfo,
    HttpProbeInfo,
    OwnerChainInfo,
)
from backend.app.tools.client import (
    REQUEST_TIMEOUT,
    KubernetesClients,
)


def _get_controller_owner(
    owner_references: list[Any] | None,
) -> Any | None:
    references = owner_references or []

    for reference in references:
        if reference.controller is True:
            return reference

    return references[0] if references else None


def _resource_values(
    values: dict[str, Any] | None,
) -> dict[str, str]:
    return {str(key): str(value) for key, value in (values or {}).items()}


def _http_probe(probe: Any | None) -> HttpProbeInfo | None:
    if probe is None or probe.http_get is None:
        return None

    http_get = probe.http_get

    return HttpProbeInfo(
        path=http_get.path,
        port=http_get.port,
        scheme=(str(http_get.scheme) if http_get.scheme is not None else None),
    )


def resolve_pod_owner(
    clients: KubernetesClients,
    namespace: str,
    pod_name: str,
) -> OwnerChainInfo:
    pod = clients.core.read_namespaced_pod(
        name=pod_name,
        namespace=namespace,
        _request_timeout=REQUEST_TIMEOUT,
    )

    direct_owner = _get_controller_owner(pod.metadata.owner_references)

    result = OwnerChainInfo(
        namespace=namespace,
        pod_name=pod_name,
    )

    if direct_owner is None:
        return result

    result.direct_owner_kind = direct_owner.kind
    result.direct_owner_name = direct_owner.name

    if direct_owner.kind == "Deployment":
        result.deployment_name = direct_owner.name
        return result

    if direct_owner.kind != "ReplicaSet":
        return result

    result.replica_set_name = direct_owner.name

    replica_set = clients.apps.read_namespaced_replica_set(
        name=direct_owner.name,
        namespace=namespace,
        _request_timeout=REQUEST_TIMEOUT,
    )

    replica_set_owner = _get_controller_owner(replica_set.metadata.owner_references)

    if replica_set_owner is not None and replica_set_owner.kind == "Deployment":
        result.deployment_name = replica_set_owner.name

    return result


def get_deployment_config(
    clients: KubernetesClients,
    namespace: str,
    deployment_name: str,
) -> DeploymentInfo:
    deployment = clients.apps.read_namespaced_deployment(
        name=deployment_name,
        namespace=namespace,
        _request_timeout=REQUEST_TIMEOUT,
    )

    containers: list[ContainerConfigInfo] = []

    for container in deployment.spec.template.spec.containers or []:
        resources = container.resources

        containers.append(
            ContainerConfigInfo(
                name=container.name,
                image=container.image,
                command=list(container.command or []),
                args=list(container.args or []),
                requests=_resource_values(
                    resources.requests if resources is not None else None
                ),
                limits=_resource_values(
                    resources.limits if resources is not None else None
                ),
                readiness_probe=_http_probe(container.readiness_probe),
                liveness_probe=_http_probe(container.liveness_probe),
            )
        )

    status = deployment.status

    return DeploymentInfo(
        namespace=namespace,
        name=deployment.metadata.name,
        desired_replicas=deployment.spec.replicas or 0,
        ready_replicas=status.ready_replicas or 0,
        available_replicas=status.available_replicas or 0,
        unavailable_replicas=status.unavailable_replicas or 0,
        selector=(deployment.spec.selector.match_labels or {}),
        template_labels=(deployment.spec.template.metadata.labels or {}),
        containers=containers,
    )
