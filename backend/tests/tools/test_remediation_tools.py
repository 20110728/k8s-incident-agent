from types import SimpleNamespace

import pytest
from kubernetes.client.exceptions import (
    ApiException,
)

from backend.app.tools.client import (
    KubernetesClients,
)
from backend.app.tools.remediation_tools import (
    UnsafeRemediationTarget,
    patch_readiness_probe,
    patch_service_selector,
)


def service_object(
    *,
    selector: dict[str, str],
    resource_version: str,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name="order-service",
            resource_version=resource_version,
        ),
        spec=SimpleNamespace(
            selector=selector,
        ),
    )


def deployment_object(
    *,
    path: str | None,
    port: str | int | None,
    resource_version: str,
    container_name: str = "order-service",
    include_probe: bool = True,
):
    readiness_probe = None

    if include_probe:
        readiness_probe = SimpleNamespace(
            http_get=SimpleNamespace(
                path=path,
                port=port,
            )
        )

    container = SimpleNamespace(
        name=container_name,
        readiness_probe=readiness_probe,
    )

    return SimpleNamespace(
        metadata=SimpleNamespace(
            name="order-service",
            resource_version=resource_version,
        ),
        spec=SimpleNamespace(
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[container],
                )
            )
        ),
    )


class FakeCoreApi:
    def __init__(
        self,
        *,
        service=None,
        read_error: Exception | None = None,
        patch_error: Exception | None = None,
    ) -> None:
        self.service = service
        self.read_error = read_error
        self.patch_error = patch_error
        self.read_calls: list[dict] = []
        self.patch_calls: list[dict] = []

    def read_namespaced_service(
        self,
        **kwargs,
    ):
        self.read_calls.append(kwargs)

        if self.read_error is not None:
            raise self.read_error

        return self.service

    def patch_namespaced_service(
        self,
        **kwargs,
    ):
        self.patch_calls.append(kwargs)

        if self.patch_error is not None:
            raise self.patch_error

        selector = kwargs["body"]["spec"][
            "selector"
        ]

        return service_object(
            selector=selector,
            resource_version="service-rv-2",
        )


class FakeAppsApi:
    def __init__(
        self,
        *,
        deployment=None,
        read_error: Exception | None = None,
        patch_error: Exception | None = None,
    ) -> None:
        self.deployment = deployment
        self.read_error = read_error
        self.patch_error = patch_error
        self.read_calls: list[dict] = []
        self.patch_calls: list[dict] = []

    def read_namespaced_deployment(
        self,
        **kwargs,
    ):
        self.read_calls.append(kwargs)

        if self.read_error is not None:
            raise self.read_error

        return self.deployment

    def patch_namespaced_deployment(
        self,
        **kwargs,
    ):
        self.patch_calls.append(kwargs)

        if self.patch_error is not None:
            raise self.patch_error

        container_data = kwargs["body"][
            "spec"
        ]["template"]["spec"]["containers"][0]

        probe_data = container_data[
            "readinessProbe"
        ]["httpGet"]

        return deployment_object(
            path=probe_data["path"],
            port=probe_data["port"],
            resource_version="deployment-rv-2",
            container_name=container_data["name"],
        )


def clients(
    *,
    core=None,
    apps=None,
) -> KubernetesClients:
    return KubernetesClients(
        core=core or FakeCoreApi(),
        apps=apps or FakeAppsApi(),
        discovery=SimpleNamespace(),
    )


def test_service_selector_patch_succeeds():
    core = FakeCoreApi(
        service=service_object(
            selector={
                "app": "wrong-service",
            },
            resource_version="service-rv-1",
        )
    )

    result = patch_service_selector(
        clients=clients(core=core),
        namespace="agent-demo",
        service_name="order-service",
        expected_selector={
            "app": "wrong-service",
        },
        proposed_selector={
            "app": "order-service",
        },
    )

    assert result.status == "succeeded"
    assert result.error_code is None
    assert len(core.patch_calls) == 1

    patch_body = core.patch_calls[0]["body"]

    assert patch_body == {
        "metadata": {
            "resourceVersion": "service-rv-1",
        },
        "spec": {
            "selector": {
                "app": "order-service",
            },
        },
    }

    assert (
        result.before_snapshot.configuration
        == {
            "selector": {
                "app": "wrong-service",
            }
        }
    )
    assert (
        result.after_snapshot.configuration
        == {
            "selector": {
                "app": "order-service",
            }
        }
    )
    assert result.rollback_patch == {
        "metadata": {
            "resourceVersion": "service-rv-2",
        },
        "spec": {
            "selector": {
                "app": "wrong-service",
            },
        },
    }


def test_service_selector_already_applied():
    core = FakeCoreApi(
        service=service_object(
            selector={
                "app": "order-service",
            },
            resource_version="service-rv-2",
        )
    )

    result = patch_service_selector(
        clients=clients(core=core),
        namespace="agent-demo",
        service_name="order-service",
        expected_selector={
            "app": "wrong-service",
        },
        proposed_selector={
            "app": "order-service",
        },
    )

    assert result.status == "already_applied"
    assert core.patch_calls == []
    assert result.applied_patch == {}
    assert result.rollback_patch == {}


def test_service_selector_conflict_does_not_patch():
    core = FakeCoreApi(
        service=service_object(
            selector={
                "app": "changed-by-someone-else",
            },
            resource_version="service-rv-3",
        )
    )

    result = patch_service_selector(
        clients=clients(core=core),
        namespace="agent-demo",
        service_name="order-service",
        expected_selector={
            "app": "wrong-service",
        },
        proposed_selector={
            "app": "order-service",
        },
    )

    assert result.status == "conflict"
    assert result.error_code == (
        "CURRENT_CONFIGURATION_CONFLICT"
    )
    assert core.patch_calls == []


def test_readiness_probe_patch_succeeds():
    apps = FakeAppsApi(
        deployment=deployment_object(
            path="/wrong-health",
            port="http",
            resource_version="deployment-rv-1",
        )
    )

    result = patch_readiness_probe(
        clients=clients(apps=apps),
        namespace="agent-demo",
        deployment_name="order-service",
        container_name="order-service",
        expected_path="/wrong-health",
        proposed_path="/healthz",
        expected_port="http",
        proposed_port="http",
    )

    assert result.status == "succeeded"
    assert result.error_code is None
    assert len(apps.patch_calls) == 1

    patch_body = apps.patch_calls[0]["body"]

    assert patch_body == {
        "metadata": {
            "resourceVersion": (
                "deployment-rv-1"
            ),
        },
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": (
                                "order-service"
                            ),
                            "readinessProbe": {
                                "httpGet": {
                                    "path": (
                                        "/healthz"
                                    ),
                                    "port": "http",
                                },
                            },
                        }
                    ],
                },
            },
        },
    }

    assert (
        result.before_snapshot.configuration
        == {
            "container_name": "order-service",
            "readiness_probe": {
                "path": "/wrong-health",
                "port": "http",
            },
        }
    )

    assert (
        result.after_snapshot.configuration
        == {
            "container_name": "order-service",
            "readiness_probe": {
                "path": "/healthz",
                "port": "http",
            },
        }
    )

    assert result.rollback_patch[
        "metadata"
    ]["resourceVersion"] == (
        "deployment-rv-2"
    )
    assert result.rollback_patch[
        "spec"
    ]["template"]["spec"]["containers"][0][
        "readinessProbe"
    ]["httpGet"] == {
        "path": "/wrong-health",
        "port": "http",
    }


def test_readiness_probe_already_applied():
    apps = FakeAppsApi(
        deployment=deployment_object(
            path="/healthz",
            port="http",
            resource_version="deployment-rv-2",
        )
    )

    result = patch_readiness_probe(
        clients=clients(apps=apps),
        namespace="agent-demo",
        deployment_name="order-service",
        container_name="order-service",
        expected_path="/wrong-health",
        proposed_path="/healthz",
        expected_port="http",
        proposed_port="http",
    )

    assert result.status == "already_applied"
    assert apps.patch_calls == []


def test_readiness_probe_conflict_does_not_patch():
    apps = FakeAppsApi(
        deployment=deployment_object(
            path="/changed-by-someone-else",
            port="http",
            resource_version="deployment-rv-3",
        )
    )

    result = patch_readiness_probe(
        clients=clients(apps=apps),
        namespace="agent-demo",
        deployment_name="order-service",
        container_name="order-service",
        expected_path="/wrong-health",
        proposed_path="/healthz",
        expected_port="http",
        proposed_port="http",
    )

    assert result.status == "conflict"
    assert result.error_code == (
        "CURRENT_CONFIGURATION_CONFLICT"
    )
    assert apps.patch_calls == []


def test_write_outside_safe_namespace_is_rejected():
    core = FakeCoreApi(
        service=service_object(
            selector={
                "app": "wrong-service",
            },
            resource_version="service-rv-1",
        )
    )

    with pytest.raises(
        UnsafeRemediationTarget,
        match="restricted to namespace",
    ):
        patch_service_selector(
            clients=clients(core=core),
            namespace="default",
            service_name="order-service",
            expected_selector={
                "app": "wrong-service",
            },
            proposed_selector={
                "app": "order-service",
            },
        )

    assert core.read_calls == []
    assert core.patch_calls == []


def test_empty_proposed_selector_is_rejected():
    core = FakeCoreApi(
        service=service_object(
            selector={
                "app": "wrong-service",
            },
            resource_version="service-rv-1",
        )
    )

    with pytest.raises(
        UnsafeRemediationTarget,
        match="must not be empty",
    ):
        patch_service_selector(
            clients=clients(core=core),
            namespace="agent-demo",
            service_name="order-service",
            expected_selector={
                "app": "wrong-service",
            },
            proposed_selector={},
        )

    assert core.read_calls == []
    assert core.patch_calls == []


def test_missing_container_is_reported():
    apps = FakeAppsApi(
        deployment=deployment_object(
            path="/wrong-health",
            port="http",
            resource_version="deployment-rv-1",
            container_name="another-container",
        )
    )

    result = patch_readiness_probe(
        clients=clients(apps=apps),
        namespace="agent-demo",
        deployment_name="order-service",
        container_name="order-service",
        expected_path="/wrong-health",
        proposed_path="/healthz",
        expected_port="http",
        proposed_port="http",
    )

    assert result.status == "failed"
    assert result.error_code == (
        "CONTAINER_NOT_FOUND"
    )
    assert apps.patch_calls == []


def test_missing_http_probe_is_reported():
    apps = FakeAppsApi(
        deployment=deployment_object(
            path=None,
            port=None,
            resource_version="deployment-rv-1",
            include_probe=False,
        )
    )

    result = patch_readiness_probe(
        clients=clients(apps=apps),
        namespace="agent-demo",
        deployment_name="order-service",
        container_name="order-service",
        expected_path="/wrong-health",
        proposed_path="/healthz",
        expected_port="http",
        proposed_port="http",
    )

    assert result.status == "failed"
    assert result.error_code == (
        "READINESS_HTTP_PROBE_NOT_FOUND"
    )
    assert apps.patch_calls == []


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (
            403,
            "KUBERNETES_FORBIDDEN",
        ),
        (
            404,
            "RESOURCE_NOT_FOUND",
        ),
        (
            409,
            "RESOURCE_VERSION_CONFLICT",
        ),
    ],
)
def test_service_patch_api_errors_are_mapped(
    status_code: int,
    expected_error: str,
):
    core = FakeCoreApi(
        service=service_object(
            selector={
                "app": "wrong-service",
            },
            resource_version="service-rv-1",
        ),
        patch_error=ApiException(
            status=status_code,
            reason="Fake Kubernetes error",
        ),
    )

    result = patch_service_selector(
        clients=clients(core=core),
        namespace="agent-demo",
        service_name="order-service",
        expected_selector={
            "app": "wrong-service",
        },
        proposed_selector={
            "app": "order-service",
        },
    )

    assert result.error_code == expected_error

    if status_code == 409:
        assert result.status == "conflict"
    else:
        assert result.status == "failed"


def test_timeout_is_mapped_without_retrying():
    core = FakeCoreApi(
        service=service_object(
            selector={
                "app": "wrong-service",
            },
            resource_version="service-rv-1",
        ),
        patch_error=TimeoutError(
            "Fake timeout"
        ),
    )

    result = patch_service_selector(
        clients=clients(core=core),
        namespace="agent-demo",
        service_name="order-service",
        expected_selector={
            "app": "wrong-service",
        },
        proposed_selector={
            "app": "order-service",
        },
    )

    assert result.status == "failed"
    assert result.error_code == (
        "KUBERNETES_TIMEOUT"
    )
    assert len(core.patch_calls) == 1