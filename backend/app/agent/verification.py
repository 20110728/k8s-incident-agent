from __future__ import annotations

import time

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from kubernetes.client.exceptions import (
    ApiException,
)
from pydantic import ValidationError

from backend.app.agent.schemas import (
    ActionExecutionResult,
    RecoveryVerificationResult,
    RemediationPlan,
    VerificationCheck,
)
from backend.app.agent.state import IncidentState
from backend.app.tools.client import (
    REQUEST_TIMEOUT,
    KubernetesClients,
)
from backend.app.tools.service_tools import (
    selector_to_string,
)


COMPLETED_ACTION_STATUSES = frozenset(
    {
        "succeeded",
        "already_applied",
    }
)


class RecoveryVerifierPort(Protocol):
    def verify(
        self,
        state: IncidentState,
    ) -> RecoveryVerificationResult:
        ...


@dataclass(frozen=True)
class VerificationObservation:
    checks: list[VerificationCheck]
    desired_replicas: int | None
    available_replicas: int | None
    ready_pods: int | None
    ready_endpoints: int | None

    @property
    def succeeded(self) -> bool:
        return bool(self.checks) and all(
            check.passed
            for check in self.checks
        )


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _label_pairs_to_dict(
    pairs,
) -> dict[str, str]:
    return {
        pair.key: pair.value
        for pair in pairs
    }


def _ready_endpoint_count(
    endpoint_slices: Any,
) -> int:
    total = 0

    for endpoint_slice in (
        getattr(
            endpoint_slices,
            "items",
            None,
        )
        or []
    ):
        for endpoint in (
            getattr(
                endpoint_slice,
                "endpoints",
                None,
            )
            or []
        ):
            conditions = getattr(
                endpoint,
                "conditions",
                None,
            )

            if (
                conditions is not None
                and getattr(
                    conditions,
                    "ready",
                    None,
                )
                is True
            ):
                total += 1

    return total


def _pod_is_ready(
    pod: Any,
) -> bool:
    status = getattr(
        pod,
        "status",
        None,
    )

    for condition in (
        getattr(
            status,
            "conditions",
            None,
        )
        or []
    ):
        if (
            getattr(
                condition,
                "type",
                None,
            )
            == "Ready"
            and getattr(
                condition,
                "status",
                None,
            )
            == "True"
        ):
            return True

    return False


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

    return list(
        getattr(
            pod_spec,
            "containers",
            None,
        )
        or []
    )


def _deployment_probe(
    deployment: Any,
    container_name: str,
) -> tuple[
    str | None,
    str | int | None,
]:
    container = next(
        (
            item
            for item in (
                _deployment_containers(
                    deployment
                )
            )
            if getattr(
                item,
                "name",
                None,
            )
            == container_name
        ),
        None,
    )

    if container is None:
        return None, None

    probe = getattr(
        container,
        "readiness_probe",
        None,
    )
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


def _deployment_selector(
    deployment: Any,
) -> dict[str, str]:
    spec = getattr(
        deployment,
        "spec",
        None,
    )
    selector = getattr(
        spec,
        "selector",
        None,
    )
    labels = getattr(
        selector,
        "match_labels",
        None,
    )

    return {
        str(key): str(value)
        for key, value in (
            labels or {}
        ).items()
    }


class KubernetesRecoveryVerifier:
    def __init__(
        self,
        *,
        clients: KubernetesClients,
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 2.0,
        monotonic: Callable[
            [],
            float,
        ] = time.monotonic,
        sleep: Callable[
            [float],
            None,
        ] = time.sleep,
        now: Callable[[], str] = utc_now,
    ) -> None:
        if timeout_seconds < 0:
            raise ValueError(
                "timeout_seconds must not "
                "be negative"
            )

        if poll_interval_seconds <= 0:
            raise ValueError(
                "poll_interval_seconds must "
                "be positive"
            )

        self._clients = clients
        self._timeout_seconds = (
            timeout_seconds
        )
        self._poll_interval_seconds = (
            poll_interval_seconds
        )
        self._monotonic = monotonic
        self._sleep = sleep
        self._now = now

    def _observe_selector(
        self,
        *,
        state: IncidentState,
        plan: RemediationPlan,
    ) -> VerificationObservation:
        parameters = plan.parameters
        proposed_selector = (
            _label_pairs_to_dict(
                parameters.proposed_selector
            )
        )

        service = (
            self._clients.core
            .read_namespaced_service(
                name=parameters.resource_name,
                namespace=parameters.namespace,
                _request_timeout=REQUEST_TIMEOUT,
            )
        )

        current_selector = {
            str(key): str(value)
            for key, value in (
                getattr(
                    service.spec,
                    "selector",
                    None,
                )
                or {}
            ).items()
        }

        request = state.get("request", {})
        service_name = request.get(
            "service_name"
        )

        if not isinstance(
            service_name,
            str,
        ) or not service_name:
            raise ValueError(
                "incident request does not contain "
                "service_name"
            )

        endpoint_slices = (
            self._clients.discovery
            .list_namespaced_endpoint_slice(
                namespace=parameters.namespace,
                label_selector=(
                    "kubernetes.io/"
                    f"service-name={service_name}"
                ),
                _request_timeout=REQUEST_TIMEOUT,
            )
        )

        ready_endpoints = (
            _ready_endpoint_count(
                endpoint_slices
            )
        )

        checks = [
            VerificationCheck(
                name="service_selector",
                passed=(
                    current_selector
                    == proposed_selector
                ),
                observed=current_selector,
                expected=proposed_selector,
                message=(
                    "Service selector matches "
                    "the approved target."
                    if current_selector
                    == proposed_selector
                    else
                    "Service selector has not "
                    "reached the approved target."
                ),
            ),
            VerificationCheck(
                name="ready_endpoints",
                passed=ready_endpoints > 0,
                observed=ready_endpoints,
                expected="at least 1",
                message=(
                    "EndpointSlice contains a "
                    "Ready endpoint."
                    if ready_endpoints > 0
                    else
                    "EndpointSlice does not contain "
                    "a Ready endpoint."
                ),
            ),
        ]

        return VerificationObservation(
            checks=checks,
            desired_replicas=None,
            available_replicas=None,
            ready_pods=None,
            ready_endpoints=ready_endpoints,
        )

    def _observe_readiness(
        self,
        *,
        state: IncidentState,
        plan: RemediationPlan,
    ) -> VerificationObservation:
        parameters = plan.parameters

        if parameters.container_name is None:
            raise ValueError(
                "readiness verification requires "
                "container_name"
            )

        if parameters.proposed_probe_path is None:
            raise ValueError(
                "readiness verification requires "
                "proposed_probe_path"
            )

        if parameters.proposed_probe_port is None:
            raise ValueError(
                "readiness verification requires "
                "proposed_probe_port"
            )

        deployment = (
            self._clients.apps
            .read_namespaced_deployment(
                name=parameters.resource_name,
                namespace=parameters.namespace,
                _request_timeout=REQUEST_TIMEOUT,
            )
        )

        observed_path, observed_port = (
            _deployment_probe(
                deployment,
                parameters.container_name,
            )
        )

        metadata = getattr(
            deployment,
            "metadata",
            None,
        )
        spec = getattr(
            deployment,
            "spec",
            None,
        )
        status = getattr(
            deployment,
            "status",
            None,
        )

        generation = (
            getattr(
                metadata,
                "generation",
                None,
            )
            or 0
        )
        observed_generation = (
            getattr(
                status,
                "observed_generation",
                None,
            )
            or 0
        )
        desired_replicas = (
            getattr(
                spec,
                "replicas",
                None,
            )
            or 0
        )
        updated_replicas = (
            getattr(
                status,
                "updated_replicas",
                None,
            )
            or 0
        )
        available_replicas = (
            getattr(
                status,
                "available_replicas",
                None,
            )
            or 0
        )

        selector = _deployment_selector(
            deployment
        )

        if selector:
            pods = (
                self._clients.core
                .list_namespaced_pod(
                    namespace=parameters.namespace,
                    label_selector=(
                        selector_to_string(
                            selector
                        )
                    ),
                    _request_timeout=(
                        REQUEST_TIMEOUT
                    ),
                )
            )
            ready_pods = sum(
                1
                for pod in (
                    getattr(
                        pods,
                        "items",
                        None,
                    )
                    or []
                )
                if _pod_is_ready(pod)
            )
        else:
            ready_pods = 0

        request = state.get("request", {})
        service_name = request.get(
            "service_name"
        )

        if not isinstance(
            service_name,
            str,
        ) or not service_name:
            raise ValueError(
                "incident request does not contain "
                "service_name"
            )

        endpoint_slices = (
            self._clients.discovery
            .list_namespaced_endpoint_slice(
                namespace=parameters.namespace,
                label_selector=(
                    "kubernetes.io/"
                    f"service-name={service_name}"
                ),
                _request_timeout=REQUEST_TIMEOUT,
            )
        )
        ready_endpoints = (
            _ready_endpoint_count(
                endpoint_slices
            )
        )

        expected_probe = {
            "path": (
                parameters.proposed_probe_path
            ),
            "port": (
                parameters.proposed_probe_port
            ),
        }
        observed_probe = {
            "path": observed_path,
            "port": observed_port,
        }

        checks = [
            VerificationCheck(
                name="readiness_probe",
                passed=(
                    observed_probe
                    == expected_probe
                ),
                observed=observed_probe,
                expected=expected_probe,
                message=(
                    "Deployment readiness probe "
                    "matches the approved target."
                    if observed_probe
                    == expected_probe
                    else
                    "Deployment readiness probe "
                    "does not match the target."
                ),
            ),
            VerificationCheck(
                name=(
                    "deployment_observed_generation"
                ),
                passed=(
                    observed_generation
                    >= generation
                ),
                observed=observed_generation,
                expected=(
                    f"at least {generation}"
                ),
                message=(
                    "Deployment controller observed "
                    "the latest generation."
                    if observed_generation
                    >= generation
                    else
                    "Deployment controller has not "
                    "observed the latest generation."
                ),
            ),
            VerificationCheck(
                name="updated_replicas",
                passed=(
                    desired_replicas > 0
                    and updated_replicas
                    >= desired_replicas
                ),
                observed=updated_replicas,
                expected=desired_replicas,
                message=(
                    "All desired replicas were "
                    "updated."
                    if (
                        desired_replicas > 0
                        and updated_replicas
                        >= desired_replicas
                    )
                    else
                    "Deployment update is incomplete."
                ),
            ),
            VerificationCheck(
                name="available_replicas",
                passed=(
                    desired_replicas > 0
                    and available_replicas
                    >= desired_replicas
                ),
                observed=available_replicas,
                expected=desired_replicas,
                message=(
                    "All desired replicas are "
                    "available."
                    if (
                        desired_replicas > 0
                        and available_replicas
                        >= desired_replicas
                    )
                    else
                    "Deployment replicas are not "
                    "fully available."
                ),
            ),
            VerificationCheck(
                name="ready_pods",
                passed=(
                    desired_replicas > 0
                    and ready_pods
                    >= desired_replicas
                ),
                observed=ready_pods,
                expected=desired_replicas,
                message=(
                    "All desired Pods are Ready."
                    if (
                        desired_replicas > 0
                        and ready_pods
                        >= desired_replicas
                    )
                    else
                    "Not all desired Pods are Ready."
                ),
            ),
            VerificationCheck(
                name="ready_endpoints",
                passed=ready_endpoints > 0,
                observed=ready_endpoints,
                expected="at least 1",
                message=(
                    "EndpointSlice contains a "
                    "Ready endpoint."
                    if ready_endpoints > 0
                    else
                    "EndpointSlice does not contain "
                    "a Ready endpoint."
                ),
            ),
        ]

        return VerificationObservation(
            checks=checks,
            desired_replicas=desired_replicas,
            available_replicas=(
                available_replicas
            ),
            ready_pods=ready_pods,
            ready_endpoints=ready_endpoints,
        )

    def _observe(
        self,
        *,
        state: IncidentState,
        plan: RemediationPlan,
    ) -> VerificationObservation:
        if (
            plan.action
            == "patch_service_selector"
        ):
            return self._observe_selector(
                state=state,
                plan=plan,
            )

        if (
            plan.action
            == "patch_readiness_probe"
        ):
            return self._observe_readiness(
                state=state,
                plan=plan,
            )

        raise ValueError(
            f"action {plan.action!r} cannot "
            "be recovery-verified"
        )

    def verify(
        self,
        state: IncidentState,
    ) -> RecoveryVerificationResult:
        started_at = self._now()

        raw_action_result = state.get(
            "action_result"
        )
        raw_plan = state.get(
            "remediation_plan"
        )

        try:
            if raw_action_result is None:
                raise ValueError(
                    "action_result is required"
                )

            if raw_plan is None:
                raise ValueError(
                    "remediation_plan is required"
                )

            action_result = (
                ActionExecutionResult.model_validate(
                    raw_action_result
                )
            )
            plan = RemediationPlan.model_validate(
                raw_plan
            )

        except (
            ValidationError,
            ValueError,
        ) as error:
            return RecoveryVerificationResult(
                execution_id=(
                    "exec-0000000000000000"
                ),
                action="manual_investigation",
                status="failed",
                started_at=started_at,
                finished_at=self._now(),
                attempts=0,
                checks=[],
                message=(
                    "Recovery verification state "
                    "is invalid."
                ),
                error_code=(
                    "INVALID_VERIFICATION_STATE"
                ),
                error_message=str(error),
            )

        if action_result.action != plan.action:
            return RecoveryVerificationResult(
                execution_id=(
                    action_result.execution_id
                ),
                action=action_result.action,
                status="failed",
                started_at=started_at,
                finished_at=self._now(),
                attempts=0,
                checks=[],
                message=(
                    "Execution result does not match "
                    "the remediation plan."
                ),
                error_code=(
                    "INVALID_VERIFICATION_STATE"
                ),
                error_message=(
                    "action_result.action does not "
                    "match remediation_plan.action"
                ),
            )

        if (
            action_result.status
            not in COMPLETED_ACTION_STATUSES
        ):
            return RecoveryVerificationResult(
                execution_id=(
                    action_result.execution_id
                ),
                action=action_result.action,
                status="skipped",
                started_at=started_at,
                finished_at=self._now(),
                attempts=0,
                checks=[],
                message=(
                    "Recovery verification was "
                    "skipped because execution "
                    "did not complete."
                ),
                error_code=None,
                error_message=None,
            )

        start_time = self._monotonic()
        deadline = (
            start_time
            + self._timeout_seconds
        )
        attempts = 0
        last_observation: (
            VerificationObservation | None
        ) = None

        try:
            while True:
                attempts += 1
                last_observation = self._observe(
                    state=state,
                    plan=plan,
                )

                if last_observation.succeeded:
                    return RecoveryVerificationResult(
                        execution_id=(
                            action_result.execution_id
                        ),
                        action=action_result.action,
                        status="succeeded",
                        started_at=started_at,
                        finished_at=self._now(),
                        attempts=attempts,
                        checks=(
                            last_observation.checks
                        ),
                        desired_replicas=(
                            last_observation
                            .desired_replicas
                        ),
                        available_replicas=(
                            last_observation
                            .available_replicas
                        ),
                        ready_pods=(
                            last_observation
                            .ready_pods
                        ),
                        ready_endpoints=(
                            last_observation
                            .ready_endpoints
                        ),
                        message=(
                            "Kubernetes recovery "
                            "verification succeeded."
                        ),
                        error_code=None,
                        error_message=None,
                    )

                if (
                    self._monotonic()
                    >= deadline
                ):
                    return RecoveryVerificationResult(
                        execution_id=(
                            action_result.execution_id
                        ),
                        action=action_result.action,
                        status="timeout",
                        started_at=started_at,
                        finished_at=self._now(),
                        attempts=attempts,
                        checks=(
                            last_observation.checks
                        ),
                        desired_replicas=(
                            last_observation
                            .desired_replicas
                        ),
                        available_replicas=(
                            last_observation
                            .available_replicas
                        ),
                        ready_pods=(
                            last_observation
                            .ready_pods
                        ),
                        ready_endpoints=(
                            last_observation
                            .ready_endpoints
                        ),
                        message=(
                            "Kubernetes recovery "
                            "verification timed out."
                        ),
                        error_code=(
                            "RECOVERY_VERIFICATION_TIMEOUT"
                        ),
                        error_message=(
                            "The approved resource "
                            "change did not recover "
                            "before the timeout."
                        ),
                    )

                self._sleep(
                    self._poll_interval_seconds
                )

        except ApiException as error:
            reason = (
                error.reason
                if isinstance(
                    error.reason,
                    str,
                )
                and error.reason
                else str(error)
            )

            return RecoveryVerificationResult(
                execution_id=(
                    action_result.execution_id
                ),
                action=action_result.action,
                status="failed",
                started_at=started_at,
                finished_at=self._now(),
                attempts=attempts,
                checks=(
                    last_observation.checks
                    if last_observation
                    is not None
                    else []
                ),
                message=(
                    "Kubernetes API recovery "
                    "verification failed."
                ),
                error_code=(
                    "KUBERNETES_VERIFICATION_ERROR"
                ),
                error_message=reason,
            )

        except Exception as error:
            return RecoveryVerificationResult(
                execution_id=(
                    action_result.execution_id
                ),
                action=action_result.action,
                status="failed",
                started_at=started_at,
                finished_at=self._now(),
                attempts=attempts,
                checks=(
                    last_observation.checks
                    if last_observation
                    is not None
                    else []
                ),
                message=(
                    "Recovery verification raised "
                    "an unexpected exception."
                ),
                error_code=(
                    "RECOVERY_VERIFICATION_ERROR"
                ),
                error_message=str(error),
            )