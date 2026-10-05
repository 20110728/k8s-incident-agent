from types import SimpleNamespace as Snapshot

import httpx
import pytest
from psycopg.errors import InvalidPassword

from backend.app.runtime.recovery import classify, transient
from backend.app.persistence.operations import approval_binding
from backend.tests.runtime.test_worker_contracts import lease


def snapshot(*, phase="evidence_collected", pending=("diagnose_incident",), tasks=(), **state):
    return Snapshot(values={"incident_id": "incident", "request": lease()["input_payload"],
                            "phase": phase, **state}, next=pending, tasks=tasks,
                    config={"configurable": {"checkpoint_id": "saved"}})


def test_empty_initial_input_and_missing_checkpoint_are_different():
    empty = Snapshot(values={}, next=(), tasks=(), config={})
    assert classify(lease(), empty).action == "start"
    assert classify({**lease(), "checkpoint_started": True}, empty).error_code == "CHECKPOINT_MISSING"


def test_continue_only_registered_read_only_nodes():
    assert classify(lease(), snapshot()).action == "continue"
    assert classify(lease(), snapshot(pending=("unknown",))).error_code == "UNSUPPORTED_PENDING_TASK"
    assert classify(lease(), snapshot(pending=("execute_remediation",))).status == "reconciling"
    assert classify(lease(), snapshot(approved=True)).status == "reconciling"


def test_checkpoint_identity_input_and_version_are_checked():
    assert classify(lease(), snapshot(incident_id="other")).error_code == "CHECKPOINT_IDENTITY_MISMATCH"
    assert classify(lease(), snapshot(request={})).error_code == "CHECKPOINT_INPUT_MISMATCH"
    assert classify({**lease(), "workflow_version": "future"}, snapshot()).error_code == "UNSUPPORTED_OR_CORRUPT_INPUT"


def test_interrupt_never_becomes_automatic_resume():
    task = Snapshot(name="request_human_approval", interrupts=("approval",), error=None)
    decision = classify(lease(), snapshot(phase="awaiting_approval", approval_status="pending", tasks=(task,)))
    assert decision.status == "waiting_approval" and decision.action == "stop"
    assert classify(lease(), snapshot(tasks=(task,))).status == "waiting_user"


def test_terminal_failure_and_unknown_task_errors_are_not_retried():
    assert classify(lease(), snapshot(phase="diagnosis_failed", pending=())).status == "failed"
    assert classify(lease(), snapshot(phase="remediation_skipped", pending=())).status == "succeeded"
    task = Snapshot(name="diagnose_incident", interrupts=(), error="do not parse this as a retry instruction")
    assert classify(lease(), snapshot(tasks=(task,))).error_code == "PENDING_TASK_FAILED"
    retry = {**lease(), "last_error": {"code": "DEPENDENCY_TEMPORARY"}}
    assert classify(retry, snapshot(tasks=(task,))).action == "continue"


@pytest.mark.parametrize("error,expected", [
    (TimeoutError(), True), (httpx.ReadTimeout("timeout"), True),
    (PermissionError(), False), (ValueError(), False), (RuntimeError(), False),
    (InvalidPassword(), False),
])
def test_error_classification(error, expected):
    assert transient(error) is expected


def test_approval_resume_requires_durable_bound_decision_and_correct_interrupt():
    task = Snapshot(name="request_human_approval", interrupts=("approval",), error=None)
    saved = snapshot(phase="awaiting_approval", approval_status="pending", approval_request={"approval_id": "apr-test"},
                     pending=("request_human_approval",), tasks=(task,))
    row = {**lease(), "approval_payload": {"binding": approval_binding(saved.values),
                                           "decision": {"approval_id": "apr-test", "approved": True}}}
    assert classify(row, saved).action == "resume"
    saved.values["evidence"] = [{"changed": True}]
    assert classify(row, saved).error_code == "APPROVAL_BINDING_MISMATCH"


def test_ledger_result_must_match_checkpoint_before_verification():
    saved = snapshot(phase="remediation_executed", pending=("verify_recovery",), approved=True,
                     approval_request={"approval_id": "apr-test"}, action_result={"status": "succeeded"})
    row = {**lease(), "approval_payload": {"binding": approval_binding(saved.values),
                                           "decision": {"approval_id": "apr-test", "approved": True}}}
    assert classify(row, saved).status == "reconciling"
    operation = {"state": "succeeded", "result": {"status": "succeeded"}}
    assert classify(row, saved, operation).action == "continue"
    assert classify(row, saved, {**operation, "state": "outcome_unknown"}).status == "reconciling"
