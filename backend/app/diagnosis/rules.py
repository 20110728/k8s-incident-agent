from collections.abc import Callable

from backend.app.schemas.diagnosis import (
    DiagnosisEvidenceReference,
    DiagnosisResult,
)
from backend.app.schemas.kubernetes import (
    PodInfo,
    ServiceEvidenceBundle,
)

DiagnosisRule = Callable[
    [ServiceEvidenceBundle],
    DiagnosisResult | None,
]


def _result(
    bundle: ServiceEvidenceBundle,
    *,
    status: str,
    fault_category: str,
    root_cause: str,
    confidence: float,
    evidence: list[DiagnosisEvidenceReference],
    recommended_action: str | None = None,
    requires_approval: bool = False,
) -> DiagnosisResult:
    return DiagnosisResult(
        namespace=bundle.namespace,
        service_name=bundle.service_name,
        status=status,
        fault_category=fault_category,
        root_cause=root_cause,
        confidence=confidence,
        evidence=evidence,
        recommended_action=recommended_action,
        requires_approval=requires_approval,
    )


def _pod_reference(
    pod: PodInfo,
    summary: str,
) -> DiagnosisEvidenceReference:
    return DiagnosisEvidenceReference(
        evidence_id=f"pod_status:{pod.name}",
        source="pod",
        resource_name=pod.name,
        summary=summary,
    )


def _ready_endpoint_count(
    bundle: ServiceEvidenceBundle,
) -> int:
    count = 0

    for endpoint_slice in bundle.endpoint_slices:
        for endpoint in endpoint_slice.endpoints:
            if endpoint.ready is True:
                count += max(len(endpoint.addresses), 1)

    return count


def service_not_found_rule(
    bundle: ServiceEvidenceBundle,
) -> DiagnosisResult | None:
    for error in bundle.errors:
        if error.operation == "get_service" and error.status_code == 404:
            return _result(
                bundle,
                status="unhealthy",
                fault_category="service_not_found",
                root_cause=(
                    f"Service {bundle.service_name!r} does not "
                    f"exist in namespace {bundle.namespace!r}."
                ),
                confidence=1.0,
                evidence=[
                    DiagnosisEvidenceReference(
                        evidence_id="collector:get_service",
                        source="collector",
                        resource_name=bundle.service_name,
                        summary="Kubernetes API returned HTTP 404.",
                    )
                ],
                recommended_action="verify_service_name",
            )

    return None


def selector_mismatch_rule(
    bundle: ServiceEvidenceBundle,
) -> DiagnosisResult | None:
    service = bundle.service

    if service is None:
        return None

    if not service.selector:
        return None

    if bundle.service_pod_names:
        return None

    if not bundle.namespace_pod_names:
        return None

    if _ready_endpoint_count(bundle) > 0:
        return None

    pod_summaries = []

    for pod in bundle.pod_statuses.values():
        pod_summaries.append(f"{pod.name} labels={pod.labels}")

    return _result(
        bundle,
        status="unhealthy",
        fault_category="selector_mismatch",
        root_cause=(
            f"Service selector {service.selector} matches no "
            "Pods, although Pods exist in the namespace."
        ),
        confidence=0.98,
        evidence=[
            DiagnosisEvidenceReference(
                evidence_id=f"service:{service.name}",
                source="service",
                resource_name=service.name,
                summary=(f"selector={service.selector}; matched_pods=0"),
            ),
            DiagnosisEvidenceReference(
                evidence_id="namespace:pods",
                source="pod",
                resource_name=bundle.namespace,
                summary="; ".join(pod_summaries),
            ),
            DiagnosisEvidenceReference(
                evidence_id="endpoint_slice:ready_count",
                source="endpoint_slice",
                resource_name=service.name,
                summary="ready_endpoint_count=0",
            ),
        ],
        recommended_action="patch_service_selector",
        requires_approval=True,
    )


def oom_killed_rule(
    bundle: ServiceEvidenceBundle,
) -> DiagnosisResult | None:
    for pod in bundle.pod_statuses.values():
        for container in pod.containers:
            reason = container.last_terminated_reason or container.terminated_reason

            if reason != "OOMKilled":
                continue

            exit_code = (
                container.last_terminated_exit_code
                if container.last_terminated_reason == "OOMKilled"
                else container.terminated_exit_code
            )

            return _result(
                bundle,
                status="unhealthy",
                fault_category="oom_killed",
                root_cause=(
                    f"Container {container.name!r} in Pod "
                    f"{pod.name!r} was terminated because it "
                    "exceeded its memory limit."
                ),
                confidence=0.99,
                evidence=[
                    _pod_reference(
                        pod,
                        (
                            f"container={container.name}; "
                            "reason=OOMKilled; "
                            f"exit_code={exit_code}; "
                            "restart_count="
                            f"{container.restart_count}"
                        ),
                    )
                ],
                recommended_action="review_memory_limit",
            )

    return None


def image_pull_rule(
    bundle: ServiceEvidenceBundle,
) -> DiagnosisResult | None:
    known_reasons = {
        "ErrImagePull",
        "ImagePullBackOff",
        "InvalidImageName",
        "RegistryUnavailable",
    }

    for pod in bundle.pod_statuses.values():
        for container in pod.containers:
            if container.waiting_reason not in known_reasons:
                continue

            return _result(
                bundle,
                status="unhealthy",
                fault_category="image_pull_error",
                root_cause=(
                    f"Container {container.name!r} in Pod "
                    f"{pod.name!r} cannot pull image "
                    f"{container.image!r}."
                ),
                confidence=0.99,
                evidence=[
                    _pod_reference(
                        pod,
                        (
                            f"container={container.name}; "
                            f"image={container.image}; "
                            "waiting_reason="
                            f"{container.waiting_reason}; "
                            "message="
                            f"{container.waiting_message}"
                        ),
                    )
                ],
                recommended_action=("verify_image_reference_and_registry"),
            )

    return None


def crash_loop_rule(
    bundle: ServiceEvidenceBundle,
) -> DiagnosisResult | None:
    for pod in bundle.pod_statuses.values():
        for container in pod.containers:
            if container.waiting_reason != "CrashLoopBackOff":
                continue

            return _result(
                bundle,
                status="unhealthy",
                fault_category="crash_loop",
                root_cause=(
                    f"Container {container.name!r} in Pod "
                    f"{pod.name!r} repeatedly exits and is "
                    "being restarted."
                ),
                confidence=0.97,
                evidence=[
                    _pod_reference(
                        pod,
                        (
                            f"container={container.name}; "
                            "waiting_reason=CrashLoopBackOff; "
                            "restart_count="
                            f"{container.restart_count}"
                        ),
                    )
                ],
                recommended_action="inspect_previous_logs",
            )

    return None


def readiness_probe_rule(
    bundle: ServiceEvidenceBundle,
) -> DiagnosisResult | None:
    for pod_name, pod in bundle.pod_statuses.items():
        if pod.phase != "Running" or pod.ready:
            continue

        relevant_event = None

        for event in bundle.pod_events.get(pod_name, []):
            message = (event.message or "").lower()

            if event.reason == "Unhealthy" and "readiness probe failed" in message:
                relevant_event = event

        if relevant_event is None:
            continue

        probe_path = None
        deployment_name = None

        owner = bundle.owner_chains.get(pod_name)

        if owner is not None:
            deployment_name = owner.deployment_name

        if deployment_name is not None and deployment_name in bundle.deployments:
            deployment = bundle.deployments[deployment_name]

            for container in deployment.containers:
                if container.readiness_probe is not None:
                    probe_path = container.readiness_probe.path
                    break

        evidence = [
            _pod_reference(
                pod,
                (
                    "phase=Running; ready=False; "
                    f"ready_endpoint_count="
                    f"{_ready_endpoint_count(bundle)}"
                ),
            ),
            DiagnosisEvidenceReference(
                evidence_id=f"event:{pod.name}:readiness",
                source="event",
                resource_name=pod.name,
                summary=relevant_event.message or "",
            ),
        ]

        if deployment_name is not None:
            evidence.append(
                DiagnosisEvidenceReference(
                    evidence_id=(f"deployment:{deployment_name}:probe"),
                    source="deployment",
                    resource_name=deployment_name,
                    summary=f"readiness_probe_path={probe_path}",
                )
            )

        return _result(
            bundle,
            status="unhealthy",
            fault_category="readiness_probe_error",
            root_cause=(
                f"Pod {pod.name!r} is running but its "
                f"readiness probe at {probe_path!r} is "
                "failing."
            ),
            confidence=0.98,
            evidence=evidence,
            recommended_action="patch_readiness_probe",
            requires_approval=True,
        )

    return None


def healthy_rule(
    bundle: ServiceEvidenceBundle,
) -> DiagnosisResult | None:
    if bundle.service is None:
        return None

    if not bundle.service_pod_names:
        return None

    service_pods = [
        bundle.pod_statuses[pod_name]
        for pod_name in bundle.service_pod_names
        if pod_name in bundle.pod_statuses
    ]

    if not service_pods:
        return None

    if not all(pod.phase == "Running" and pod.ready for pod in service_pods):
        return None

    ready_endpoints = _ready_endpoint_count(bundle)

    if ready_endpoints == 0:
        return None

    return _result(
        bundle,
        status="healthy",
        fault_category="healthy",
        root_cause=(
            "Service has running and ready Pods and at least "
            "one ready EndpointSlice address."
        ),
        confidence=1.0,
        evidence=[
            DiagnosisEvidenceReference(
                evidence_id=(f"service:{bundle.service.name}:healthy"),
                source="service",
                resource_name=bundle.service.name,
                summary=(
                    f"ready_pods={len(service_pods)}; ready_endpoints={ready_endpoints}"
                ),
            )
        ],
    )


def unknown_rule(
    bundle: ServiceEvidenceBundle,
) -> DiagnosisResult:
    error_summary = "; ".join(
        f"{error.operation}: {error.message}" for error in bundle.errors[:3]
    )

    root_cause = "The collected evidence does not match a supported diagnostic rule."

    if error_summary:
        root_cause += f" Evidence collection errors: {error_summary}"

    return _result(
        bundle,
        status="unknown",
        fault_category="unknown",
        root_cause=root_cause,
        confidence=0.0,
        evidence=[],
        recommended_action="manual_investigation",
    )


RULES: tuple[DiagnosisRule, ...] = (
    service_not_found_rule,
    selector_mismatch_rule,
    oom_killed_rule,
    image_pull_rule,
    crash_loop_rule,
    readiness_probe_rule,
    healthy_rule,
)
