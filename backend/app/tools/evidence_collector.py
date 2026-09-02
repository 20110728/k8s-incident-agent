from kubernetes.client.exceptions import ApiException

from backend.app.schemas.kubernetes import (
    CollectionErrorInfo,
    ServiceEvidenceBundle,
)
from backend.app.tools.client import KubernetesClients
from backend.app.tools.node_tools import get_node_status
from backend.app.tools.pod_tools import (
    get_pod_events,
    get_pod_logs,
    get_pod_status,
)
from backend.app.tools.service_tools import (
    get_service,
    get_service_endpoint_slices,
    list_namespace_pod_names,
    list_service_pod_names,
)
from backend.app.tools.workload_tools import (
    get_deployment_config,
    resolve_pod_owner,
)


def _record_error(
    bundle: ServiceEvidenceBundle,
    operation: str,
    resource_kind: str,
    resource_name: str,
    error: Exception,
) -> None:
    status_code = None

    if isinstance(error, ApiException):
        status_code = error.status

    bundle.errors.append(
        CollectionErrorInfo(
            operation=operation,
            resource_kind=resource_kind,
            resource_name=resource_name,
            message=str(error),
            status_code=status_code,
        )
    )


def _pod_may_have_logs(pod_status: object) -> bool:
    containers = getattr(pod_status, "containers", [])

    return any(
        container.state in {"running", "terminated"} or container.restart_count > 0
        for container in containers
    )


def _pod_is_abnormal(pod_status: object) -> bool:
    phase = getattr(pod_status, "phase", "Unknown")
    ready = getattr(pod_status, "ready", False)
    containers = getattr(pod_status, "containers", [])

    return (
        phase != "Running"
        or not ready
        or any(container.restart_count > 0 for container in containers)
    )


def collect_service_evidence(
    clients: KubernetesClients,
    namespace: str,
    service_name: str,
) -> ServiceEvidenceBundle:
    bundle = ServiceEvidenceBundle(
        namespace=namespace,
        service_name=service_name,
    )

    try:
        bundle.namespace_pod_names = list_namespace_pod_names(
            clients=clients,
            namespace=namespace,
        )
    except Exception as error:
        _record_error(
            bundle=bundle,
            operation="list_namespace_pods",
            resource_kind="Namespace",
            resource_name=namespace,
            error=error,
        )

    try:
        bundle.service = get_service(
            clients=clients,
            namespace=namespace,
            service_name=service_name,
        )
    except Exception as error:
        _record_error(
            bundle=bundle,
            operation="get_service",
            resource_kind="Service",
            resource_name=service_name,
            error=error,
        )

    if bundle.service is not None:
        try:
            bundle.service_pod_names = list_service_pod_names(
                clients=clients,
                namespace=namespace,
                service_name=service_name,
            )
        except Exception as error:
            _record_error(
                bundle=bundle,
                operation="list_service_pods",
                resource_kind="Service",
                resource_name=service_name,
                error=error,
            )

        try:
            bundle.endpoint_slices = get_service_endpoint_slices(
                clients=clients,
                namespace=namespace,
                service_name=service_name,
            )
        except Exception as error:
            _record_error(
                bundle=bundle,
                operation="get_endpoint_slices",
                resource_kind="Service",
                resource_name=service_name,
                error=error,
            )

    pod_names = (
        bundle.service_pod_names
        if bundle.service_pod_names
        else bundle.namespace_pod_names
    )

    for pod_name in pod_names:
        pod_status = None

        try:
            pod_status = get_pod_status(
                clients=clients,
                namespace=namespace,
                pod_name=pod_name,
            )
            bundle.pod_statuses[pod_name] = pod_status
        except Exception as error:
            _record_error(
                bundle=bundle,
                operation="get_pod_status",
                resource_kind="Pod",
                resource_name=pod_name,
                error=error,
            )

        try:
            bundle.pod_events[pod_name] = get_pod_events(
                clients=clients,
                namespace=namespace,
                pod_name=pod_name,
            )
        except Exception as error:
            _record_error(
                bundle=bundle,
                operation="get_pod_events",
                resource_kind="Pod",
                resource_name=pod_name,
                error=error,
            )

        if (
            pod_status is not None
            and _pod_is_abnormal(pod_status)
            and _pod_may_have_logs(pod_status)
        ):
            try:
                bundle.pod_logs.append(
                    get_pod_logs(
                        clients=clients,
                        namespace=namespace,
                        pod_name=pod_name,
                        tail_lines=100,
                    )
                )
            except Exception as error:
                _record_error(
                    bundle=bundle,
                    operation="get_current_logs",
                    resource_kind="Pod",
                    resource_name=pod_name,
                    error=error,
                )

            has_restarts = any(
                container.restart_count > 0 for container in pod_status.containers
            )

            if has_restarts:
                try:
                    bundle.pod_logs.append(
                        get_pod_logs(
                            clients=clients,
                            namespace=namespace,
                            pod_name=pod_name,
                            previous=True,
                            tail_lines=100,
                        )
                    )
                except Exception as error:
                    _record_error(
                        bundle=bundle,
                        operation="get_previous_logs",
                        resource_kind="Pod",
                        resource_name=pod_name,
                        error=error,
                    )

        try:
            owner_chain = resolve_pod_owner(
                clients=clients,
                namespace=namespace,
                pod_name=pod_name,
            )
            bundle.owner_chains[pod_name] = owner_chain
        except Exception as error:
            _record_error(
                bundle=bundle,
                operation="resolve_pod_owner",
                resource_kind="Pod",
                resource_name=pod_name,
                error=error,
            )
            owner_chain = None

        if (
            owner_chain is not None
            and owner_chain.deployment_name is not None
            and owner_chain.deployment_name not in bundle.deployments
        ):
            deployment_name = owner_chain.deployment_name

            try:
                bundle.deployments[deployment_name] = get_deployment_config(
                    clients=clients,
                    namespace=namespace,
                    deployment_name=deployment_name,
                )
            except Exception as error:
                _record_error(
                    bundle=bundle,
                    operation="get_deployment_config",
                    resource_kind="Deployment",
                    resource_name=deployment_name,
                    error=error,
                )

        if (
            pod_status is not None
            and pod_status.node_name is not None
            and pod_status.node_name not in bundle.nodes
        ):
            node_name = pod_status.node_name

            try:
                bundle.nodes[node_name] = get_node_status(
                    clients=clients,
                    node_name=node_name,
                )
            except Exception as error:
                _record_error(
                    bundle=bundle,
                    operation="get_node_status",
                    resource_kind="Node",
                    resource_name=node_name,
                    error=error,
                )

    return bundle
