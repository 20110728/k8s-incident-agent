from types import SimpleNamespace

from kubernetes.client.exceptions import ApiException

import backend.app.tools.evidence_collector as collector
from backend.app.schemas.kubernetes import (
    ContainerStatusInfo,
    DeploymentInfo,
    NodeInfo,
    OwnerChainInfo,
    PodInfo,
    PodLogInfo,
    ServiceInfo,
)


def test_selector_mismatch_falls_back_to_namespace_pods(
    monkeypatch,
):
    clients = SimpleNamespace()

    monkeypatch.setattr(
        collector,
        "list_namespace_pod_names",
        lambda **_: ["pod-1"],
    )

    monkeypatch.setattr(
        collector,
        "get_service",
        lambda **_: ServiceInfo(
            namespace="agent-demo",
            name="order-service",
            service_type="ClusterIP",
            selector={"app": "wrong-service"},
        ),
    )

    monkeypatch.setattr(
        collector,
        "list_service_pod_names",
        lambda **_: [],
    )

    monkeypatch.setattr(
        collector,
        "get_service_endpoint_slices",
        lambda **_: [],
    )

    monkeypatch.setattr(
        collector,
        "get_pod_status",
        lambda **_: PodInfo(
            namespace="agent-demo",
            name="pod-1",
            phase="Running",
            ready=True,
            labels={"app": "order-service"},
        ),
    )

    monkeypatch.setattr(
        collector,
        "get_pod_events",
        lambda **_: [],
    )

    monkeypatch.setattr(
        collector,
        "resolve_pod_owner",
        lambda **_: OwnerChainInfo(
            namespace="agent-demo",
            pod_name="pod-1",
        ),
    )

    bundle = collector.collect_service_evidence(
        clients=clients,
        namespace="agent-demo",
        service_name="order-service",
    )

    assert bundle.service_pod_names == []
    assert bundle.namespace_pod_names == ["pod-1"]
    assert "pod-1" in bundle.pod_statuses
    assert bundle.errors == []


def test_abnormal_restarted_pod_collects_previous_logs(
    monkeypatch,
):
    clients = SimpleNamespace()

    pod_status = PodInfo(
        namespace="agent-demo",
        name="pod-1",
        phase="Running",
        ready=False,
        node_name="worker-1",
        containers=[
            ContainerStatusInfo(
                name="order-service",
                ready=False,
                restart_count=2,
                image="nginx:1.30.4-alpine",
                state="waiting",
                waiting_reason="CrashLoopBackOff",
            )
        ],
    )

    monkeypatch.setattr(
        collector,
        "list_namespace_pod_names",
        lambda **_: ["pod-1"],
    )

    monkeypatch.setattr(
        collector,
        "get_service",
        lambda **_: ServiceInfo(
            namespace="agent-demo",
            name="order-service",
            service_type="ClusterIP",
            selector={"app": "order-service"},
        ),
    )

    monkeypatch.setattr(
        collector,
        "list_service_pod_names",
        lambda **_: ["pod-1"],
    )

    monkeypatch.setattr(
        collector,
        "get_service_endpoint_slices",
        lambda **_: [],
    )

    monkeypatch.setattr(
        collector,
        "get_pod_status",
        lambda **_: pod_status,
    )

    monkeypatch.setattr(
        collector,
        "get_pod_events",
        lambda **_: [],
    )

    def fake_logs(**arguments):
        return PodLogInfo(
            namespace="agent-demo",
            pod_name="pod-1",
            previous=arguments.get("previous", False),
            truncated=False,
            content="test log",
        )

    monkeypatch.setattr(
        collector,
        "get_pod_logs",
        fake_logs,
    )

    monkeypatch.setattr(
        collector,
        "resolve_pod_owner",
        lambda **_: OwnerChainInfo(
            namespace="agent-demo",
            pod_name="pod-1",
            replica_set_name="order-service-rs",
            deployment_name="order-service",
        ),
    )

    monkeypatch.setattr(
        collector,
        "get_deployment_config",
        lambda **_: DeploymentInfo(
            namespace="agent-demo",
            name="order-service",
            desired_replicas=1,
            ready_replicas=0,
            available_replicas=0,
            unavailable_replicas=1,
        ),
    )

    monkeypatch.setattr(
        collector,
        "get_node_status",
        lambda **_: NodeInfo(
            name="worker-1",
            ready=True,
        ),
    )

    bundle = collector.collect_service_evidence(
        clients=clients,
        namespace="agent-demo",
        service_name="order-service",
    )

    assert len(bundle.pod_logs) == 2
    assert bundle.pod_logs[0].previous is False
    assert bundle.pod_logs[1].previous is True
    assert "order-service" in bundle.deployments
    assert "worker-1" in bundle.nodes
    assert bundle.errors == []


def test_service_api_error_is_recorded(monkeypatch):
    clients = SimpleNamespace()

    monkeypatch.setattr(
        collector,
        "list_namespace_pod_names",
        lambda **_: [],
    )

    def raise_not_found(**_):
        raise ApiException(
            status=404,
            reason="Not Found",
        )

    monkeypatch.setattr(
        collector,
        "get_service",
        raise_not_found,
    )

    bundle = collector.collect_service_evidence(
        clients=clients,
        namespace="agent-demo",
        service_name="missing-service",
    )

    assert bundle.service is None
    assert len(bundle.errors) == 1
    assert bundle.errors[0].operation == "get_service"
    assert bundle.errors[0].status_code == 404
