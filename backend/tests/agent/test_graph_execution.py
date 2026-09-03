from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import (
    InMemorySaver,
)
from langgraph.types import Command

from backend.app.agent.execution_policy import (
    build_execution_id,
)
from backend.app.agent.graph import (
    build_incident_graph,
)
from backend.app.agent.schemas import (
    ActionExecutionResult,
    RecoveryVerificationResult,
)
from backend.tests.agent.fakes import (
    FakeCollector,
    FakeDiagnoser,
    FakeRecoveryVerifier,
    FakeRemediationExecutor,
    FakeRemediationPlanner,
    FakeRetriever,
)
from backend.tests.agent.test_graph_human_approval import (
    initial_state,
    manual_plan,
    selector_diagnosis,
    selector_mismatch_bundle,
    selector_patch_plan,
    selector_runbooks,
)


def build_execution_graph(
    remediation_plan,
):
    collector = FakeCollector(
        result=selector_mismatch_bundle()
    )
    retriever = FakeRetriever(
        result=selector_runbooks()
    )
    diagnoser = FakeDiagnoser(
        diagnosis=selector_diagnosis()
    )
    planner = FakeRemediationPlanner(
        plan=remediation_plan
    )
    executor = FakeRemediationExecutor()
    verifier = FakeRecoveryVerifier()

    graph = build_incident_graph(
        collector=collector,
        retriever=retriever,
        diagnoser=diagnoser,
        planner=planner,
        executor=executor,
        verifier=verifier,
        checkpointer=InMemorySaver(),
    )

    return {
        "graph": graph,
        "collector": collector,
        "retriever": retriever,
        "diagnoser": diagnoser,
        "planner": planner,
        "executor": executor,
        "verifier": verifier,
    }


def pause_graph(components):
    config = {
        "configurable": {
            "thread_id": str(uuid4()),
        }
    }

    paused = components[
        "graph"
    ].invoke(
        initial_state(),
        config=config,
    )

    assert "__interrupt__" in paused

    approval_request = paused[
        "__interrupt__"
    ][0].value["approval_request"]

    return (
        config,
        approval_request,
    )


def make_action_result(
    *,
    approval_id: str,
    status: str,
) -> ActionExecutionResult:
    return ActionExecutionResult(
        execution_id=build_execution_id(
            approval_id
        ),
        approval_id=approval_id,
        action="patch_service_selector",
        status=status,
        namespace="agent-demo",
        resource_kind="Service",
        resource_name="order-service",
        started_at=(
            "2026-09-03T12:01:00+00:00"
        ),
        finished_at=(
            "2026-09-03T12:01:01+00:00"
        ),
        before_snapshot=None,
        after_snapshot=None,
        applied_patch={},
        rollback_patch={},
        message=(
            f"Fake execution {status}."
        ),
        error_code=(
            f"FAKE_{status.upper()}"
            if status in {
                "failed",
                "conflict",
            }
            else None
        ),
        error_message=(
            f"Fake {status} error."
            if status in {
                "failed",
                "conflict",
            }
            else None
        ),
    )


def make_verification_result(
    *,
    execution_id: str,
    status: str,
) -> RecoveryVerificationResult:
    return RecoveryVerificationResult(
        execution_id=execution_id,
        action="patch_service_selector",
        status=status,
        started_at=(
            "2026-09-03T12:02:00+00:00"
        ),
        finished_at=(
            "2026-09-03T12:02:01+00:00"
        ),
        attempts=1,
        checks=[],
        desired_replicas=None,
        available_replicas=None,
        ready_pods=None,
        ready_endpoints=(
            1
            if status == "succeeded"
            else 0
        ),
        message=(
            f"Fake verification {status}."
        ),
        error_code=(
            "RECOVERY_VERIFICATION_TIMEOUT"
            if status == "timeout"
            else None
        ),
        error_message=(
            "Fake verification timeout."
            if status == "timeout"
            else None
        ),
    )


def resume(
    components,
    *,
    config,
    approval_request,
    approved: bool,
):
    return components[
        "graph"
    ].invoke(
        Command(
            resume={
                "approval_id": (
                    approval_request[
                        "approval_id"
                    ]
                ),
                "approved": approved,
                "approver": "test-operator",
                "comment": (
                    "Day 14 Graph测试"
                ),
            }
        ),
        config=config,
    )


def test_approved_execution_and_verification_succeed():
    components = build_execution_graph(
        selector_patch_plan()
    )
    config, request = pause_graph(
        components
    )

    action_result = make_action_result(
        approval_id=request["approval_id"],
        status="succeeded",
    )
    verification_result = (
        make_verification_result(
            execution_id=(
                action_result.execution_id
            ),
            status="succeeded",
        )
    )

    components["executor"].result = (
        action_result
    )
    components["verifier"].result = (
        verification_result
    )

    result = resume(
        components,
        config=config,
        approval_request=request,
        approved=True,
    )

    assert result["phase"] == (
        "verification_succeeded"
    )
    assert result["approved"] is True
    assert result["action_result"].status == (
        "succeeded"
    )
    assert (
        result["verification_result"].status
        == "succeeded"
    )
    assert result["errors"] == []

    assert len(
        components["executor"].calls
    ) == 1
    assert len(
        components["verifier"].calls
    ) == 1

    assert components["collector"].calls == [
        (
            "agent-demo",
            "order-service",
        )
    ]
    assert len(
        components["retriever"].queries
    ) == 1
    assert len(
        components["diagnoser"].calls
    ) == 1
    assert len(
        components["planner"].calls
    ) == 1


def test_rejected_approval_does_not_execute():
    components = build_execution_graph(
        selector_patch_plan()
    )
    config, request = pause_graph(
        components
    )

    result = resume(
        components,
        config=config,
        approval_request=request,
        approved=False,
    )

    assert result["phase"] == (
        "approval_rejected"
    )
    assert result["approved"] is False
    assert components["executor"].calls == []
    assert components["verifier"].calls == []
    assert result.get("action_result") is None
    assert (
        result.get("verification_result")
        is None
    )


@pytest.mark.parametrize(
    (
        "execution_status",
        "expected_phase",
    ),
    [
        (
            "conflict",
            "remediation_execution_conflict",
        ),
        (
            "failed",
            "remediation_execution_failed",
        ),
    ],
)
def test_failed_execution_does_not_verify(
    execution_status: str,
    expected_phase: str,
):
    components = build_execution_graph(
        selector_patch_plan()
    )
    config, request = pause_graph(
        components
    )

    components["executor"].result = (
        make_action_result(
            approval_id=(
                request["approval_id"]
            ),
            status=execution_status,
        )
    )

    result = resume(
        components,
        config=config,
        approval_request=request,
        approved=True,
    )

    assert result["phase"] == expected_phase
    assert (
        result["action_result"].status
        == execution_status
    )
    assert len(
        components["executor"].calls
    ) == 1
    assert components["verifier"].calls == []
    assert (
        result.get("verification_result")
        is None
    )


def test_verification_timeout_is_terminal():
    components = build_execution_graph(
        selector_patch_plan()
    )
    config, request = pause_graph(
        components
    )

    action_result = make_action_result(
        approval_id=request["approval_id"],
        status="succeeded",
    )

    components["executor"].result = (
        action_result
    )
    components["verifier"].result = (
        make_verification_result(
            execution_id=(
                action_result.execution_id
            ),
            status="timeout",
        )
    )

    result = resume(
        components,
        config=config,
        approval_request=request,
        approved=True,
    )

    assert result["phase"] == (
        "verification_failed"
    )
    assert (
        result["verification_result"].status
        == "timeout"
    )
    assert result["errors"][-1]["code"] == (
        "RECOVERY_VERIFICATION_TIMEOUT"
    )


def test_verifier_exception_is_recorded():
    components = build_execution_graph(
        selector_patch_plan()
    )
    config, request = pause_graph(
        components
    )

    action_result = make_action_result(
        approval_id=request["approval_id"],
        status="succeeded",
    )

    components["executor"].result = (
        action_result
    )
    components["verifier"].error = (
        RuntimeError(
            "fake verifier failure"
        )
    )

    result = resume(
        components,
        config=config,
        approval_request=request,
        approved=True,
    )

    assert result["phase"] == (
        "verification_failed"
    )
    assert result["errors"][-1]["code"] == (
        "RECOVERY_VERIFICATION_ERROR"
    )
    assert (
        result["errors"][-1]["message"]
        == "fake verifier failure"
    )


def test_manual_plan_does_not_execute():
    components = build_execution_graph(
        manual_plan()
    )

    config = {
        "configurable": {
            "thread_id": str(uuid4()),
        }
    }

    result = components[
        "graph"
    ].invoke(
        initial_state(),
        config=config,
    )

    assert "__interrupt__" not in result
    assert result["phase"] == (
        "remediation_planned"
    )
    assert result["requires_approval"] is False
    assert components["executor"].calls == []
    assert components["verifier"].calls == []


def test_executor_requires_planner():
    with pytest.raises(
        ValueError,
        match="executor requires a planner",
    ):
        build_incident_graph(
            collector=object(),
            executor=object(),
        )


def test_verifier_requires_executor():
    with pytest.raises(
        ValueError,
        match="verifier requires an executor",
    ):
        build_incident_graph(
            collector=object(),
            verifier=object(),
        )