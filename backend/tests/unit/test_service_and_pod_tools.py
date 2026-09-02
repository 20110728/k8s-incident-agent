from types import SimpleNamespace
from unittest.mock import Mock

from backend.app.schemas.kubernetes import ServiceInfo
from backend.app.tools.pod_tools import (
    get_pod_logs,
    get_pod_status,
)
from backend.app.tools.service_tools import (
    get_service_endpoint_slices,
    list_service_pod_names,
    selector_to_string,
)


def ns(**values):
    return SimpleNamespace(**values)


def make_clients():
    return ns(
        core=Mock(),
        apps=Mock(),
        discovery=Mock(),
    )


def test_selector_to_string_sorts_keys():
    selector = {
        "version": "v1",
        "app": "order-service",
    }

    assert selector_to_string(selector) == ("app=order-service,version=v1")


def test_service_without_selector_returns_no_pods(
    monkeypatch,
):
    clients = make_clients()

    monkeypatch.setattr(
        "backend.app.tools.service_tools.get_service",
        lambda **_: ServiceInfo(
            namespace="agent-demo",
            name="order-service",
            service_type="ClusterIP",
            selector={},
        ),
    )

    result = list_service_pod_names(
        clients=clients,
        namespace="agent-demo",
        service_name="order-service",
    )

    assert result == []
    clients.core.list_namespaced_pod.assert_not_called()


def test_list_service_pods_uses_selector(monkeypatch):
    clients = make_clients()

    monkeypatch.setattr(
        "backend.app.tools.service_tools.get_service",
        lambda **_: ServiceInfo(
            namespace="agent-demo",
            name="order-service",
            service_type="ClusterIP",
            selector={"app": "order-service"},
        ),
    )

    clients.core.list_namespaced_pod.return_value = ns(
        items=[
            ns(metadata=ns(name="pod-b")),
            ns(metadata=ns(name="pod-a")),
        ]
    )

    result = list_service_pod_names(
        clients=clients,
        namespace="agent-demo",
        service_name="order-service",
    )

    assert result == ["pod-a", "pod-b"]

    clients.core.list_namespaced_pod.assert_called_once()

    call_arguments = clients.core.list_namespaced_pod.call_args.kwargs

    assert call_arguments["label_selector"] == ("app=order-service")


def test_endpoint_slice_preserves_ready_status():
    clients = make_clients()

    endpoint = ns(
        addresses=["10.244.1.2"],
        conditions=ns(
            ready=False,
            serving=False,
            terminating=False,
        ),
        node_name="incident-agent-worker",
        target_ref=ns(
            kind="Pod",
            name="order-service-test",
        ),
    )

    endpoint_slice = ns(
        metadata=ns(name="order-service-test"),
        address_type="IPv4",
        endpoints=[endpoint],
    )

    clients.discovery.list_namespaced_endpoint_slice.return_value = ns(
        items=[endpoint_slice]
    )

    result = get_service_endpoint_slices(
        clients=clients,
        namespace="agent-demo",
        service_name="order-service",
    )

    assert len(result) == 1
    assert result[0].endpoints[0].ready is False
    assert result[0].endpoints[0].target_name == ("order-service-test")


def test_pod_status_extracts_oom_killed():
    clients = make_clients()

    current_state = ns(
        running=None,
        waiting=ns(
            reason="CrashLoopBackOff",
            message="back-off restarting failed container",
        ),
        terminated=None,
    )

    last_state = ns(
        running=None,
        waiting=None,
        terminated=ns(
            reason="OOMKilled",
            exit_code=137,
        ),
    )

    container_status = ns(
        name="order-service",
        ready=False,
        restart_count=3,
        image="nginx:1.30.4-alpine",
        state=current_state,
        last_state=last_state,
    )

    clients.core.read_namespaced_pod.return_value = ns(
        metadata=ns(
            name="order-service-test",
            labels={"app": "order-service"},
        ),
        spec=ns(
            node_name="incident-agent-worker",
        ),
        status=ns(
            phase="Running",
            pod_ip="10.244.1.2",
            conditions=[
                ns(
                    type="Ready",
                    status="False",
                )
            ],
            container_statuses=[container_status],
        ),
    )

    result = get_pod_status(
        clients=clients,
        namespace="agent-demo",
        pod_name="order-service-test",
    )

    assert result.ready is False
    assert result.containers[0].waiting_reason == ("CrashLoopBackOff")
    assert result.containers[0].last_terminated_reason == ("OOMKilled")
    assert result.containers[0].last_terminated_exit_code == 137


def test_pod_logs_are_truncated():
    clients = make_clients()

    clients.core.read_namespaced_pod_log.return_value = "x" * 25_000

    result = get_pod_logs(
        clients=clients,
        namespace="agent-demo",
        pod_name="order-service-test",
        tail_lines=10_000,
    )

    assert result.truncated is True
    assert len(result.content) == 20_000

    call_arguments = clients.core.read_namespaced_pod_log.call_args.kwargs

    assert call_arguments["tail_lines"] == 500
