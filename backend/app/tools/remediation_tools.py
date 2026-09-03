from __future__ import annotations

import re

from typing import Any

from kubernetes.client.exceptions import (
    ApiException,
)

from backend.app.agent.remediation_policy import (
    SAFE_NAMESPACE,
)
from backend.app.agent.schemas import (
    KUBERNETES_NAME_PATTERN,
    ResourceMutationResult,
    ResourceSnapshot,
)
from backend.app.tools.client import (
    REQUEST_TIMEOUT,
    KubernetesClients,
)


KUBERNETES_NAME_REGEX = re.compile(
    KUBERNETES_NAME_PATTERN
)


class UnsafeRemediationTarget(ValueError):
    """Raised before any Kubernetes write is attempted."""


def _validate_resource_name(
    value: str,
    *,
    field_name: str,
) -> str:
    normalized = value.strip()

    if KUBERNETES_NAME_REGEX.fullmatch(
        normalized
    ) is None:
        raise UnsafeRemediationTarget(
            f"{field_name} is not a valid "
            "Kubernetes resource name"
        )

    return normalized


def _validate_namespace(
    namespace: str,
) -> str:
    normalized = namespace.strip()

    if normalized != SAFE_NAMESPACE:
        raise UnsafeRemediationTarget(
            f"write operations are restricted to "
            f"namespace {SAFE_NAMESPACE!r}"
        )

    return normalized


def _validate_selector(
    selector: dict[str, str],
    *,
    field_name: str,
    allow_empty: bool,
) -> dict[str, str]:
    if not isinstance(selector, dict):
        raise UnsafeRemediationTarget(
            f"{field_name} must be a dictionary"
        )

    normalized: dict[str, str] = {}

    for key, value in selector.items():
        if not isinstance(key, str):
            raise UnsafeRemediationTarget(
                f"{field_name} contains a non-string key"
            )

        if not isinstance(value, str):
            raise UnsafeRemediationTarget(
                f"{field_name} contains a non-string value"
            )

        normalized_key = key.strip()
        normalized_value = value.strip()

        if not normalized_key:
            raise UnsafeRemediationTarget(
                f"{field_name} contains an empty key"
            )

        if not normalized_value:
            raise UnsafeRemediationTarget(
                f"{field_name} contains an empty value"
            )

        normalized[
            normalized_key
        ] = normalized_value

    if not allow_empty and not normalized:
        raise UnsafeRemediationTarget(
            f"{field_name} must not be empty"
        )

    return normalized


def _validate_probe_path(
    value: str,
    *,
    field_name: str,
) -> str:
    if not isinstance(value, str):
        raise UnsafeRemediationTarget(
            f"{field_name} must be a string"
        )

    normalized = value.strip()

    if not normalized.startswith("/"):
        raise UnsafeRemediationTarget(
            f"{field_name} must start with '/'"
        )

    if any(
        character.isspace()
        for character in normalized
    ):
        raise UnsafeRemediationTarget(
            f"{field_name} must not contain whitespace"
        )

    return normalized


def _validate_probe_port(
    value: str | int,
    *,
    field_name: str,
) -> str | int:
    if isinstance(value, bool):
        raise UnsafeRemediationTarget(
            f"{field_name} must be a string or integer"
        )

    if isinstance(value, int):
        if value <= 0 or value > 65535:
            raise UnsafeRemediationTarget(
                f"{field_name} integer is outside "
                "the valid port range"
            )

        return value

    if isinstance(value, str):
        normalized = value.strip()

        if not normalized:
            raise UnsafeRemediationTarget(
                f"{field_name} must not be empty"
            )

        return normalized

    raise UnsafeRemediationTarget(
        f"{field_name} must be a string or integer"
    )


def _resource_version(
    resource: Any,
) -> str:
    metadata = getattr(
        resource,
        "metadata",
        None,
    )
    value = getattr(
        metadata,
        "resource_version",
        None,
    )

    if not isinstance(value, str) or not value:
        raise ValueError(
            "Kubernetes resource does not contain "
            "metadata.resourceVersion"
        )

    return value


def _service_selector(
    service: Any,
) -> dict[str, str]:
    spec = getattr(
        service,
        "spec",
        None,
    )
    selector = getattr(
        spec,
        "selector",
        None,
    )

    if selector is None:
        return {}

    return {
        str(key): str(value)
        for key, value in selector.items()
    }


def _deployment_containers(
    deployment: Any,
) -> list[Any]:
    spec = getattr(
        deployment,
        "spec",
        None,
    )
    template = getattr(
        spec,
        "template",
        None,
    )
    pod_spec = getattr(
        template,
        "spec",
        None,
    )
    containers = getattr(
        pod_spec,
        "containers",
        None,
    )

    return list(containers or [])


def _find_container(
    deployment: Any,
    container_name: str,
) -> Any | None:
    return next(
        (
            container
            for container in (
                _deployment_containers(
                    deployment
                )
            )
            if getattr(
                container,
                "name",
                None,
            )
            == container_name
        ),
        None,
    )


def _readiness_probe_values(
    container: Any,
) -> tuple[str | None, str | int | None]:
    probe = getattr(
        container,
        "readiness_probe",
        None,
    )

    if probe is None:
        return None, None

    http_get = getattr(
        probe,
        "http_get",
        None,
    )

    if http_get is None:
        return None, None

    return (
        getattr(
            http_get,
            "path",
            None,
        ),
        getattr(
            http_get,
            "port",
            None,
        ),
    )


def _service_snapshot(
    *,
    namespace: str,
    service_name: str,
    service: Any,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        namespace=namespace,
        resource_kind="Service",
        resource_name=service_name,
        resource_version=(
            _resource_version(service)
        ),
        configuration={
            "selector": _service_selector(
                service
            ),
        },
    )


def _deployment_snapshot(
    *,
    namespace: str,
    deployment_name: str,
    deployment: Any,
    container_name: str,
) -> ResourceSnapshot:
    container = _find_container(
        deployment,
        container_name,
    )

    if container is None:
        raise ValueError(
            "container does not exist in Deployment"
        )

    path, port = _readiness_probe_values(
        container
    )

    return ResourceSnapshot(
        namespace=namespace,
        resource_kind="Deployment",
        resource_name=deployment_name,
        resource_version=(
            _resource_version(deployment)
        ),
        configuration={
            "container_name": container_name,
            "readiness_probe": {
                "path": path,
                "port": port,
            },
        },
    )


def _failed_result(
    *,
    error_code: str,
    error_message: str,
    before_snapshot: (
        ResourceSnapshot | None
    ) = None,
    applied_patch: (
        dict[str, Any] | None
    ) = None,
) -> ResourceMutationResult:
    return ResourceMutationResult(
        status="failed",
        before_snapshot=before_snapshot,
        after_snapshot=None,
        applied_patch=(
            applied_patch or {}
        ),
        rollback_patch={},
        message="Kubernetes remediation failed.",
        error_code=error_code,
        error_message=error_message,
    )


def _conflict_result(
    *,
    error_code: str,
    error_message: str,
    before_snapshot: (
        ResourceSnapshot | None
    ),
    applied_patch: (
        dict[str, Any] | None
    ) = None,
) -> ResourceMutationResult:
    return ResourceMutationResult(
        status="conflict",
        before_snapshot=before_snapshot,
        after_snapshot=None,
        applied_patch=(
            applied_patch or {}
        ),
        rollback_patch={},
        message=(
            "Kubernetes resource changed after "
            "the remediation plan was approved."
        ),
        error_code=error_code,
        error_message=error_message,
    )


def _api_error_result(
    error: ApiException,
    *,
    before_snapshot: (
        ResourceSnapshot | None
    ) = None,
    applied_patch: (
        dict[str, Any] | None
    ) = None,
) -> ResourceMutationResult:
    status = error.status

    if status == 409:
        return _conflict_result(
            error_code=(
                "RESOURCE_VERSION_CONFLICT"
            ),
            error_message=(
                "Kubernetes rejected the patch "
                "because resourceVersion changed."
            ),
            before_snapshot=before_snapshot,
            applied_patch=applied_patch,
        )

    error_codes = {
        403: "KUBERNETES_FORBIDDEN",
        404: "RESOURCE_NOT_FOUND",
    }

    error_code = error_codes.get(
        status,
        "KUBERNETES_API_ERROR",
    )

    reason = (
        error.reason
        if isinstance(error.reason, str)
        and error.reason
        else "Kubernetes API request failed."
    )

    return _failed_result(
        error_code=error_code,
        error_message=reason,
        before_snapshot=before_snapshot,
        applied_patch=applied_patch,
    )


def _unexpected_error_result(
    error: Exception,
    *,
    before_snapshot: (
        ResourceSnapshot | None
    ) = None,
    applied_patch: (
        dict[str, Any] | None
    ) = None,
) -> ResourceMutationResult:
    if isinstance(error, TimeoutError):
        error_code = "KUBERNETES_TIMEOUT"
    else:
        error_code = "KUBERNETES_API_ERROR"

    return _failed_result(
        error_code=error_code,
        error_message=str(error),
        before_snapshot=before_snapshot,
        applied_patch=applied_patch,
    )


def patch_service_selector(
    *,
    clients: KubernetesClients,
    namespace: str,
    service_name: str,
    expected_selector: dict[str, str],
    proposed_selector: dict[str, str],
) -> ResourceMutationResult:
    safe_namespace = _validate_namespace(
        namespace
    )
    safe_service_name = (
        _validate_resource_name(
            service_name,
            field_name="service_name",
        )
    )
    safe_expected_selector = (
        _validate_selector(
            expected_selector,
            field_name="expected_selector",
            allow_empty=True,
        )
    )
    safe_proposed_selector = (
        _validate_selector(
            proposed_selector,
            field_name="proposed_selector",
            allow_empty=False,
        )
    )

    try:
        service = (
            clients.core.read_namespaced_service(
                name=safe_service_name,
                namespace=safe_namespace,
                _request_timeout=REQUEST_TIMEOUT,
            )
        )
    except ApiException as error:
        return _api_error_result(error)
    except Exception as error:
        return _unexpected_error_result(
            error
        )

    try:
        before_snapshot = _service_snapshot(
            namespace=safe_namespace,
            service_name=safe_service_name,
            service=service,
        )
    except Exception as error:
        return _failed_result(
            error_code=(
                "INVALID_KUBERNETES_RESPONSE"
            ),
            error_message=str(error),
        )

    current_selector = (
        before_snapshot.configuration[
            "selector"
        ]
    )

    if current_selector == (
        safe_proposed_selector
    ):
        return ResourceMutationResult(
            status="already_applied",
            before_snapshot=before_snapshot,
            after_snapshot=before_snapshot,
            applied_patch={},
            rollback_patch={},
            message=(
                "Service selector already matches "
                "the approved target."
            ),
            error_code=None,
            error_message=None,
        )

    if current_selector != (
        safe_expected_selector
    ):
        return _conflict_result(
            error_code=(
                "CURRENT_CONFIGURATION_CONFLICT"
            ),
            error_message=(
                "Current Service selector does not "
                "match the approved previous value."
            ),
            before_snapshot=before_snapshot,
        )

    patch_body = {
        "metadata": {
            "resourceVersion": (
                before_snapshot.resource_version
            ),
        },
        "spec": {
            "selector": safe_proposed_selector,
        },
    }

    try:
        patched_service = (
            clients.core.patch_namespaced_service(
                name=safe_service_name,
                namespace=safe_namespace,
                body=patch_body,
                _content_type=(
                    "application/merge-patch+json"
                ),
                _request_timeout=REQUEST_TIMEOUT,
            )
        )
    except ApiException as error:
        return _api_error_result(
            error,
            before_snapshot=before_snapshot,
            applied_patch=patch_body,
        )
    except Exception as error:
        return _unexpected_error_result(
            error,
            before_snapshot=before_snapshot,
            applied_patch=patch_body,
        )

    try:
        after_snapshot = _service_snapshot(
            namespace=safe_namespace,
            service_name=safe_service_name,
            service=patched_service,
        )
    except Exception as error:
        return _failed_result(
            error_code=(
                "INVALID_KUBERNETES_RESPONSE"
            ),
            error_message=str(error),
            before_snapshot=before_snapshot,
            applied_patch=patch_body,
        )

    rollback_patch = {
        "metadata": {
            "resourceVersion": (
                after_snapshot.resource_version
            ),
        },
        "spec": {
            "selector": current_selector,
        },
    }

    return ResourceMutationResult(
        status="succeeded",
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        applied_patch=patch_body,
        rollback_patch=rollback_patch,
        message=(
            "Service selector was patched "
            "successfully."
        ),
        error_code=None,
        error_message=None,
    )


def patch_readiness_probe(
    *,
    clients: KubernetesClients,
    namespace: str,
    deployment_name: str,
    container_name: str,
    expected_path: str,
    proposed_path: str,
    expected_port: str | int,
    proposed_port: str | int,
) -> ResourceMutationResult:
    safe_namespace = _validate_namespace(
        namespace
    )
    safe_deployment_name = (
        _validate_resource_name(
            deployment_name,
            field_name="deployment_name",
        )
    )
    safe_container_name = (
        _validate_resource_name(
            container_name,
            field_name="container_name",
        )
    )
    safe_expected_path = (
        _validate_probe_path(
            expected_path,
            field_name="expected_path",
        )
    )
    safe_proposed_path = (
        _validate_probe_path(
            proposed_path,
            field_name="proposed_path",
        )
    )
    safe_expected_port = (
        _validate_probe_port(
            expected_port,
            field_name="expected_port",
        )
    )
    safe_proposed_port = (
        _validate_probe_port(
            proposed_port,
            field_name="proposed_port",
        )
    )

    try:
        deployment = (
            clients.apps.read_namespaced_deployment(
                name=safe_deployment_name,
                namespace=safe_namespace,
                _request_timeout=REQUEST_TIMEOUT,
            )
        )
    except ApiException as error:
        return _api_error_result(error)
    except Exception as error:
        return _unexpected_error_result(
            error
        )

    container = _find_container(
        deployment,
        safe_container_name,
    )

    if container is None:
        return _failed_result(
            error_code="CONTAINER_NOT_FOUND",
            error_message=(
                "Container does not exist in "
                "the current Deployment."
            ),
        )

    current_path, current_port = (
        _readiness_probe_values(container)
    )

    if (
        current_path is None
        or current_port is None
    ):
        return _failed_result(
            error_code=(
                "READINESS_HTTP_PROBE_NOT_FOUND"
            ),
            error_message=(
                "Container does not have an HTTP "
                "readiness probe."
            ),
        )

    try:
        before_snapshot = (
            _deployment_snapshot(
                namespace=safe_namespace,
                deployment_name=(
                    safe_deployment_name
                ),
                deployment=deployment,
                container_name=(
                    safe_container_name
                ),
            )
        )
    except Exception as error:
        return _failed_result(
            error_code=(
                "INVALID_KUBERNETES_RESPONSE"
            ),
            error_message=str(error),
        )

    if (
        current_path == safe_proposed_path
        and current_port == safe_proposed_port
    ):
        return ResourceMutationResult(
            status="already_applied",
            before_snapshot=before_snapshot,
            after_snapshot=before_snapshot,
            applied_patch={},
            rollback_patch={},
            message=(
                "Readiness probe already matches "
                "the approved target."
            ),
            error_code=None,
            error_message=None,
        )

    if (
        current_path != safe_expected_path
        or current_port != safe_expected_port
    ):
        return _conflict_result(
            error_code=(
                "CURRENT_CONFIGURATION_CONFLICT"
            ),
            error_message=(
                "Current readiness probe does not "
                "match the approved previous value."
            ),
            before_snapshot=before_snapshot,
        )

    patch_body = {
        "metadata": {
            "resourceVersion": (
                before_snapshot.resource_version
            ),
        },
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": (
                                safe_container_name
                            ),
                            "readinessProbe": {
                                "httpGet": {
                                    "path": (
                                        safe_proposed_path
                                    ),
                                    "port": (
                                        safe_proposed_port
                                    ),
                                },
                            },
                        }
                    ],
                },
            },
        },
    }

    try:
        patched_deployment = (
            clients.apps.patch_namespaced_deployment(
                name=safe_deployment_name,
                namespace=safe_namespace,
                body=patch_body,
                _content_type=(
                    "application/"
                    "strategic-merge-patch+json"
                ),
                _request_timeout=REQUEST_TIMEOUT,
            )
        )
    except ApiException as error:
        return _api_error_result(
            error,
            before_snapshot=before_snapshot,
            applied_patch=patch_body,
        )
    except Exception as error:
        return _unexpected_error_result(
            error,
            before_snapshot=before_snapshot,
            applied_patch=patch_body,
        )

    try:
        after_snapshot = (
            _deployment_snapshot(
                namespace=safe_namespace,
                deployment_name=(
                    safe_deployment_name
                ),
                deployment=patched_deployment,
                container_name=(
                    safe_container_name
                ),
            )
        )
    except Exception as error:
        return _failed_result(
            error_code=(
                "INVALID_KUBERNETES_RESPONSE"
            ),
            error_message=str(error),
            before_snapshot=before_snapshot,
            applied_patch=patch_body,
        )

    rollback_patch = {
        "metadata": {
            "resourceVersion": (
                after_snapshot.resource_version
            ),
        },
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": (
                                safe_container_name
                            ),
                            "readinessProbe": {
                                "httpGet": {
                                    "path": current_path,
                                    "port": current_port,
                                },
                            },
                        }
                    ],
                },
            },
        },
    }

    return ResourceMutationResult(
        status="succeeded",
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        applied_patch=patch_body,
        rollback_patch=rollback_patch,
        message=(
            "Deployment readiness probe was "
            "patched successfully."
        ),
        error_code=None,
        error_message=None,
    )