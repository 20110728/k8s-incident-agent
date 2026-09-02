from types import SimpleNamespace
from unittest.mock import Mock

from backend.app.tools.node_tools import get_node_status
from backend.app.tools.workload_tools import (
    get_deployment_config,
    resolve_pod_owner,
)


def ns(**values):
    return SimpleNamespace(**values)


def make_clients():
    return ns(
        core=Mock(),
        apps=Mock(),
        discovery=Mock(),
    )


def test_resolve_pod_to_deployment():
    clients = make_clients()

    clients.core.read_namespaced_pod.return_value = ns(
        metadata=ns(
            owner_references=[
                ns(
                    controller=True,
                    kind="ReplicaSet",
                    name="order-service-rs",
                )
            ]
        )
    )

    clients.apps.read_namespaced_replica_set.return_value = ns(
        metadata=ns(
            owner_references=[
                ns(
                    controller=True,
                    kind="Deployment",
                    name="order-service",
                )
            ]
        )
    )

    result = resolve_pod_owner(
        clients=clients,
        namespace="agent-demo",
        pod_name="order-service-pod",
    )

    assert result.direct_owner_kind == "ReplicaSet"
    assert result.replica_set_name == "order-service-rs"
    assert result.deployment_name == "order-service"


def test_deployment_config_extracts_probe_and_limits():
    clients = make_clients()

    probe = ns(
        http_get=ns(
            path="/healthz",
            port="http",
            scheme="HTTP",
        )
    )

    container = ns(
        name="order-service",
        image="nginx:1.30.4-alpine",
        command=None,
        args=None,
        readiness_probe=probe,
        liveness_probe=probe,
        resources=ns(
            requests={
                "cpu": "20m",
                "memory": "32Mi",
            },
            limits={
                "cpu": "200m",
                "memory": "128Mi",
            },
        ),
    )

    clients.apps.read_namespaced_deployment.return_value = ns(
        metadata=ns(name="order-service"),
        spec=ns(
            replicas=2,
            selector=ns(match_labels={"app": "order-service"}),
            template=ns(
                metadata=ns(labels={"app": "order-service"}),
                spec=ns(containers=[container]),
            ),
        ),
        status=ns(
            ready_replicas=2,
            available_replicas=2,
            unavailable_replicas=None,
        ),
    )

    result = get_deployment_config(
        clients=clients,
        namespace="agent-demo",
        deployment_name="order-service",
    )

    assert result.ready_replicas == 2
    assert result.containers[0].readiness_probe.path == ("/healthz")
    assert result.containers[0].limits["memory"] == "128Mi"


def test_node_ready_status():
    clients = make_clients()

    clients.core.read_node.return_value = ns(
        metadata=ns(name="incident-agent-worker"),
        status=ns(
            capacity={
                "cpu": "4",
                "memory": "8Gi",
            },
            allocatable={
                "cpu": "3900m",
                "memory": "7Gi",
            },
            conditions=[
                ns(
                    type="Ready",
                    status="True",
                    reason="KubeletReady",
                    message="kubelet is posting ready status",
                )
            ],
        ),
    )

    result = get_node_status(
        clients=clients,
        node_name="incident-agent-worker",
    )

    assert result.ready is True
    assert result.capacity["cpu"] == "4"
    assert result.conditions[0].reason == "KubeletReady"
