import pytest

from backend.app.diagnosis.engine import diagnose_evidence
from backend.app.schemas.kubernetes import (
    CollectionErrorInfo,
    ContainerStatusInfo,
    EndpointInfo,
    EndpointSliceInfo,
    EventInfo,
    PodInfo,
    ServiceEvidenceBundle,
    ServiceInfo,
)


def make_bundle() -> ServiceEvidenceBundle:
    return ServiceEvidenceBundle(
        namespace="agent-demo",
        service_name="order-service",
        service=ServiceInfo(
            namespace="agent-demo",
            name="order-service",
            service_type="ClusterIP",
            selector={"app": "order-service"},
        ),
    )


def make_pod(
    *,
    ready: bool,
    waiting_reason: str | None = None,
    last_reason: str | None = None,
    exit_code: int | None = None,
    restart_count: int = 0,
) -> PodInfo:
    return PodInfo(
        namespace="agent-demo",
        name="pod-1",
        phase="Running",
        ready=ready,
        labels={"app": "order-service"},
        containers=[
            ContainerStatusInfo(
                name="order-service",
                ready=ready,
                restart_count=restart_count,
                image="nginx:test",
                state=("running" if ready else "waiting"),
                waiting_reason=waiting_reason,
                last_terminated_reason=last_reason,
                last_terminated_exit_code=exit_code,
            )
        ],
    )


def test_service_not_found():
    bundle = ServiceEvidenceBundle(
        namespace="agent-demo",
        service_name="missing-service",
        errors=[
            CollectionErrorInfo(
                operation="get_service",
                resource_kind="Service",
                resource_name="missing-service",
                message="Not Found",
                status_code=404,
            )
        ],
    )

    result = diagnose_evidence(bundle)

    assert result.fault_category == "service_not_found"
    assert result.confidence == 1.0


def test_selector_mismatch():
    bundle = make_bundle()
    bundle.service.selector = {"app": "wrong-service"}
    bundle.service_pod_names = []
    bundle.namespace_pod_names = ["pod-1"]
    bundle.pod_statuses["pod-1"] = make_pod(ready=True)

    result = diagnose_evidence(bundle)

    assert result.fault_category == "selector_mismatch"
    assert result.requires_approval is True
    assert result.recommended_action == ("patch_service_selector")


def test_oom_has_priority_over_crash_loop():
    bundle = make_bundle()
    bundle.service_pod_names = ["pod-1"]
    bundle.pod_statuses["pod-1"] = make_pod(
        ready=False,
        waiting_reason="CrashLoopBackOff",
        last_reason="OOMKilled",
        exit_code=137,
        restart_count=3,
    )

    result = diagnose_evidence(bundle)

    assert result.fault_category == "oom_killed"
    assert result.confidence == 0.99


@pytest.mark.parametrize(
    "reason",
    [
        "ErrImagePull",
        "ImagePullBackOff",
        "InvalidImageName",
    ],
)
def test_image_pull_errors(reason):
    bundle = make_bundle()
    bundle.service_pod_names = ["pod-1"]
    bundle.pod_statuses["pod-1"] = make_pod(
        ready=False,
        waiting_reason=reason,
    )

    result = diagnose_evidence(bundle)

    assert result.fault_category == "image_pull_error"


def test_crash_loop():
    bundle = make_bundle()
    bundle.service_pod_names = ["pod-1"]
    bundle.pod_statuses["pod-1"] = make_pod(
        ready=False,
        waiting_reason="CrashLoopBackOff",
        restart_count=4,
    )

    result = diagnose_evidence(bundle)

    assert result.fault_category == "crash_loop"
    assert result.recommended_action == ("inspect_previous_logs")


def test_readiness_probe_error():
    bundle = make_bundle()
    bundle.service_pod_names = ["pod-1"]
    bundle.pod_statuses["pod-1"] = make_pod(
        ready=False,
    )

    bundle.pod_events["pod-1"] = [
        EventInfo(
            namespace="agent-demo",
            resource_name="pod-1",
            event_type="Warning",
            reason="Unhealthy",
            message=("Readiness probe failed: HTTP probe failed with statuscode: 404"),
        )
    ]

    result = diagnose_evidence(bundle)

    assert result.fault_category == ("readiness_probe_error")
    assert result.requires_approval is True


def test_healthy_service():
    bundle = make_bundle()
    bundle.service_pod_names = ["pod-1"]
    bundle.namespace_pod_names = ["pod-1"]
    bundle.pod_statuses["pod-1"] = make_pod(ready=True)

    bundle.endpoint_slices = [
        EndpointSliceInfo(
            namespace="agent-demo",
            name="order-service-test",
            service_name="order-service",
            address_type="IPv4",
            endpoints=[
                EndpointInfo(
                    addresses=["10.244.1.2"],
                    ready=True,
                )
            ],
        )
    ]

    result = diagnose_evidence(bundle)

    assert result.fault_category == "healthy"
    assert result.status == "healthy"
    assert result.confidence == 1.0


def test_unknown_fault():
    bundle = make_bundle()

    result = diagnose_evidence(bundle)

    assert result.fault_category == "unknown"
    assert result.status == "unknown"
    assert result.confidence == 0.0
