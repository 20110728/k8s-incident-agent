import json
from typing import Any

from backend.app.tools.client import create_clients
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

NAMESPACE = "agent-demo"
SERVICE_NAME = "order-service"


def serialize(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()

    if isinstance(value, list):
        return [serialize(item) for item in value]

    return value


def main() -> None:
    clients = create_clients()

    service = get_service(
        clients=clients,
        namespace=NAMESPACE,
        service_name=SERVICE_NAME,
    )

    service_pods = list_service_pod_names(
        clients=clients,
        namespace=NAMESPACE,
        service_name=SERVICE_NAME,
    )

    namespace_pods = list_namespace_pod_names(
        clients=clients,
        namespace=NAMESPACE,
    )

    endpoint_slices = get_service_endpoint_slices(
        clients=clients,
        namespace=NAMESPACE,
        service_name=SERVICE_NAME,
    )

    pod_results = []

    for pod_name in service_pods:
        status = get_pod_status(
            clients=clients,
            namespace=NAMESPACE,
            pod_name=pod_name,
        )

        events = get_pod_events(
            clients=clients,
            namespace=NAMESPACE,
            pod_name=pod_name,
        )

        logs = get_pod_logs(
            clients=clients,
            namespace=NAMESPACE,
            pod_name=pod_name,
            tail_lines=20,
        )

        pod_results.append(
            {
                "status": serialize(status),
                "events": serialize(events),
                "logs": serialize(logs),
            }
        )

    result = {
        "service": serialize(service),
        "service_pods": service_pods,
        "namespace_pods": namespace_pods,
        "endpoint_slices": serialize(endpoint_slices),
        "pods": pod_results,
    }

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
