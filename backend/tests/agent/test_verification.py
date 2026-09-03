from types import SimpleNamespace

from kubernetes.client.exceptions import (
    ApiException,
)

from backend.app.agent.schemas import (
    ActionExecutionResult,
    LabelPair,
    RemediationParameters,
    RemediationPlan,
)
from backend.app.agent.verification import (
    KubernetesRecoveryVerifier,
)
from backend.app.tools.client import (
    KubernetesClients,
)


def service_object(
    selector: dict[str, str],
):
    return SimpleNamespace(
        spec=SimpleNamespace(
            selector=selector,
        )
    )


def endpoint_slices(
    ready_values: list[bool],
):
    endpoints = [
        SimpleNamespace(
            conditions=SimpleNamespace(
                ready=value,
            )
        )
        for value in ready_values
    ]

    return SimpleNamespace(
        items=[
            SimpleNamespace(
                endpoints=endpoints,
            )
        ]
    )


def pod_object(
    ready: bool,
):
    return SimpleNamespace(
        status=SimpleNamespace(
            conditions=[
                SimpleNamespace(
                    type="Ready",
                    status=(
                        "True"
                        if ready
                        else "False"
                    ),
                )
            ]
        )
    )


def deployment_object(
    *,
    path: str,
    port: str | int,
    desired: int,
    updated: int,
    available: int,
    generation: int,
    observed_generation: int,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            generation=generation,
        ),
        spec=SimpleNamespace(
            replicas=desired,
            selector=SimpleNamespace(
                match_labels={
                    "app": "order-service",
                }
            ),
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[
                        SimpleNamespace(
                            name=(
                                "order-service"
                            ),
                            readiness_probe=(
                                SimpleNamespace(
                                    http_get=(
                                        SimpleNamespace(
                                            path=path,
                                            port=port,
                                        )
                                    )
                                )
                            ),
                        )
                    ]
                )
            ),
        ),
        status=SimpleNamespace(
            observed_generation=(
                observed_generation
            ),
            updated_replicas=updated,
            available_replicas=available,
        ),
    )


class FakeCoreApi:
    def __init__(
        self,
        *,
        service=None,
        pods=None,
        error: Exception | None = None,
    ) -> None:
        self.service = service
        self.pods = pods or []
        self.error = error
        self.service_calls: list[dict] = []
        self.pod_calls: list[dict] = []

    def read_namespaced_service(
        self,
        **kwargs,
    ):
        self.service_calls.append(kwargs)

        if self.error is not None:
            raise self.error

        return self.service

    def list_namespaced_pod(
        self,
        **kwargs,
    ):
        self.pod_calls.append(kwargs)

        if self.error is not None:
            raise self.error

        return SimpleNamespace(
            items=self.pods,
        )


class FakeAppsApi:
    def __init__(
        self,
        *,
        deployment=None,
        error: Exception | None = None,
    ) -> None:
        self.deployment = deployment
        self.error = error
        self.calls: list[dict] = []

    def read_namespaced_deployment(
        self,
        **kwargs,
    ):
        self.calls.append(kwargs)

        if self.error is not None:
            raise self.error

        return self.deployment


class FakeDiscoveryApi:
    def __init__(
        self,
        *,
        slices=None,
        error: Exception | None = None,
    ) -> None:
        self.slices = slices
        self.error = error
        self.calls: list[dict] = []

    def list_namespaced_endpoint_slice(
        self,
        **kwargs,
    ):
        self.calls.append(kwargs)

        if self.error is not None:
            raise self.error

        return self.slices


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(
        self,
        seconds: float,
    ) -> None:
        self.sleep_calls.append(seconds)
        self.value += seconds


def now_source():
    values = iter(
        [
            "2026-09-03T12:00:00+00:00",
            "2026-09-03T12:01:00+00:00",
        ]
    )

    return lambda: next(values)


def selector_plan() -> RemediationPlan:
    return RemediationPlan(
        action="patch_service_selector",
        parameters=RemediationParameters(
            namespace="agent-demo",
            resource_kind="Service",
            resource_name="order-service",
            container_name=None,
            current_probe_path=None,
            proposed_probe_path=None,
            current_probe_port=None,
            proposed_probe_port=None,
            current_selector=[
                LabelPair(
                    key="app",
                    value="wrong-service",
                )
            ],
            proposed_selector=[
                LabelPair(
                    key="app",
                    value="order-service",
                )
            ],
            investigation_steps=[],
        ),
        risk_level="medium",
        summary="修复Selector。",
        expected_result="恢复Endpoint。",
        rollback_plan="恢复旧Selector。",
        evidence_ids=["ev-test-001"],
        runbook_ids=[
            "selector-label-mismatch"
        ],
        requires_approval=True,
    )


def readiness_plan() -> RemediationPlan:
    return RemediationPlan(
        action="patch_readiness_probe",
        parameters=RemediationParameters(
            namespace="agent-demo",
            resource_kind="Deployment",
            resource_name="order-service",
            container_name="order-service",
            current_probe_path=(
                "/wrong-health"
            ),
            proposed_probe_path="/healthz",
            current_probe_port="http",
            proposed_probe_port="http",
            current_selector=[],
            proposed_selector=[],
            investigation_steps=[],
        ),
        risk_level="medium",
        summary="修复Readiness Probe。",
        expected_result="Pod恢复Ready。",
        rollback_plan="恢复旧Probe。",
        evidence_ids=["ev-test-001"],
        runbook_ids=["wrong-http-path"],
        requires_approval=True,
    )


def execution_result(
    plan: RemediationPlan,
    *,
    status: str = "succeeded",
) -> ActionExecutionResult:
    return ActionExecutionResult(
        execution_id=(
            "exec-0123456789abcdef"
        ),
        approval_id=(
            "apr-0123456789abcdef"
        ),
        action=plan.action,
        status=status,
        namespace=(
            plan.parameters.namespace
        ),
        resource_kind=(
            plan.parameters.resource_kind
        ),
        resource_name=(
            plan.parameters.resource_name
        ),
        started_at=(
            "2026-09-03T11:59:00+00:00"
        ),
        finished_at=(
            "2026-09-03T11:59:01+00:00"
        ),
        before_snapshot=None,
        after_snapshot=None,
        applied_patch={},
        rollback_patch={},
        message="Fake execution.",
        error_code=None,
        error_message=None,
    )


def state_for(
    plan: RemediationPlan,
    *,
    status: str = "succeeded",
) -> dict:
    return {
        "request": {
            "namespace": "agent-demo",
            "service_name": "order-service",
            "description": "测试恢复验证",
        },
        "remediation_plan": plan,
        "action_result": execution_result(
            plan,
            status=status,
        ),
    }


def clients(
    *,
    core,
    apps,
    discovery,
) -> KubernetesClients:
    return KubernetesClients(
        core=core,
        apps=apps,
        discovery=discovery,
    )


def test_selector_verification_succeeds():
    plan = selector_plan()
    core = FakeCoreApi(
        service=service_object(
            {
                "app": "order-service",
            }
        )
    )
    discovery = FakeDiscoveryApi(
        slices=endpoint_slices([True])
    )

    verifier = KubernetesRecoveryVerifier(
        clients=clients(
            core=core,
            apps=FakeAppsApi(),
            discovery=discovery,
        ),
        now=now_source(),
    )

    result = verifier.verify(
        state_for(plan)
    )

    assert result.status == "succeeded"
    assert result.attempts == 1
    assert result.ready_endpoints == 1
    assert all(
        check.passed
        for check in result.checks
    )
    assert len(core.service_calls) == 1
    assert len(discovery.calls) == 1


def test_selector_verification_times_out():
    plan = selector_plan()
    clock = FakeClock()

    core = FakeCoreApi(
        service=service_object(
            {
                "app": "order-service",
            }
        )
    )
    discovery = FakeDiscoveryApi(
        slices=endpoint_slices([])
    )

    verifier = KubernetesRecoveryVerifier(
        clients=clients(
            core=core,
            apps=FakeAppsApi(),
            discovery=discovery,
        ),
        timeout_seconds=2.0,
        poll_interval_seconds=1.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        now=now_source(),
    )

    result = verifier.verify(
        state_for(plan)
    )

    assert result.status == "timeout"
    assert result.attempts == 3
    assert result.ready_endpoints == 0
    assert result.error_code == (
        "RECOVERY_VERIFICATION_TIMEOUT"
    )
    assert clock.sleep_calls == [
        1.0,
        1.0,
    ]


def test_selector_api_error_is_reported():
    plan = selector_plan()

    core = FakeCoreApi(
        error=ApiException(
            status=500,
            reason="Fake API failure",
        )
    )

    verifier = KubernetesRecoveryVerifier(
        clients=clients(
            core=core,
            apps=FakeAppsApi(),
            discovery=FakeDiscoveryApi(),
        ),
        now=now_source(),
    )

    result = verifier.verify(
        state_for(plan)
    )

    assert result.status == "failed"
    assert result.attempts == 1
    assert result.error_code == (
        "KUBERNETES_VERIFICATION_ERROR"
    )


def test_readiness_verification_succeeds():
    plan = readiness_plan()

    core = FakeCoreApi(
        pods=[
            pod_object(True),
            pod_object(True),
        ]
    )
    apps = FakeAppsApi(
        deployment=deployment_object(
            path="/healthz",
            port="http",
            desired=2,
            updated=2,
            available=2,
            generation=5,
            observed_generation=5,
        )
    )
    discovery = FakeDiscoveryApi(
        slices=endpoint_slices([True, True])
    )

    verifier = KubernetesRecoveryVerifier(
        clients=clients(
            core=core,
            apps=apps,
            discovery=discovery,
        ),
        now=now_source(),
    )

    result = verifier.verify(
        state_for(plan)
    )

    assert result.status == "succeeded"
    assert result.attempts == 1
    assert result.desired_replicas == 2
    assert result.available_replicas == 2
    assert result.ready_pods == 2
    assert result.ready_endpoints == 2
    assert all(
        check.passed
        for check in result.checks
    )


def test_readiness_verification_times_out():
    plan = readiness_plan()
    clock = FakeClock()

    core = FakeCoreApi(
        pods=[
            pod_object(True),
            pod_object(False),
        ]
    )
    apps = FakeAppsApi(
        deployment=deployment_object(
            path="/healthz",
            port="http",
            desired=2,
            updated=2,
            available=1,
            generation=5,
            observed_generation=5,
        )
    )
    discovery = FakeDiscoveryApi(
        slices=endpoint_slices([])
    )

    verifier = KubernetesRecoveryVerifier(
        clients=clients(
            core=core,
            apps=apps,
            discovery=discovery,
        ),
        timeout_seconds=2.0,
        poll_interval_seconds=1.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        now=now_source(),
    )

    result = verifier.verify(
        state_for(plan)
    )

    assert result.status == "timeout"
    assert result.attempts == 3
    assert result.available_replicas == 1
    assert result.ready_pods == 1
    assert result.ready_endpoints == 0


def test_verification_is_skipped_after_failure():
    plan = selector_plan()

    verifier = KubernetesRecoveryVerifier(
        clients=clients(
            core=FakeCoreApi(),
            apps=FakeAppsApi(),
            discovery=FakeDiscoveryApi(),
        ),
        now=now_source(),
    )

    result = verifier.verify(
        state_for(
            plan,
            status="failed",
        )
    )

    assert result.status == "skipped"
    assert result.attempts == 0


def test_action_mismatch_is_rejected():
    plan = selector_plan()
    state = state_for(plan)

    state["action_result"] = (
        state["action_result"].model_copy(
            update={
                "action": (
                    "patch_readiness_probe"
                ),
                "resource_kind": "Deployment",
            }
        )
    )

    verifier = KubernetesRecoveryVerifier(
        clients=clients(
            core=FakeCoreApi(),
            apps=FakeAppsApi(),
            discovery=FakeDiscoveryApi(),
        ),
        now=now_source(),
    )

    result = verifier.verify(state)

    assert result.status == "failed"
    assert result.attempts == 0
    assert result.error_code == (
        "INVALID_VERIFICATION_STATE"
    )