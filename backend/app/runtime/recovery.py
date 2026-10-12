"""Classify saved state before choosing START, read-only continuation, or no execution."""
from dataclasses import dataclass
from typing import Literal

import httpx
from openai import APIConnectionError, APIStatusError
from psycopg import OperationalError

from backend.app.persistence.runs import request_digest
from backend.app.persistence.operations import approval_binding, json_value
from backend.app.services.round_context import ROUND_WORKFLOWS, DIALOGUE_WORKFLOW, INVESTIGATION_WORKFLOW


READ_ONLY_NODES = frozenset({
    "validate_request", "plan_collection", "collect_evidence", "retrieve_runbooks",
    "diagnose_incident", "plan_remediation", "skip_remediation", "prepare_approval",
    "request_human_approval", "finish_failure", "prepare_clarification", "await_user_input",
})


@dataclass(frozen=True)
class RecoveryDecision:
    action: Literal["start", "continue", "resume", "resume_user", "stop"]
    status: str | None = None
    error_code: str | None = None


def transient(error: Exception) -> bool:
    if isinstance(error, OperationalError):
        sqlstate = error.sqlstate
        return sqlstate is None or sqlstate.startswith("08") or sqlstate in {"53300", "57P01", "57P02", "57P03"}
    if isinstance(error, (TimeoutError, ConnectionError, httpx.TransportError,
                          APIConnectionError)):
        return True
    return isinstance(error, APIStatusError) and (error.status_code in {408, 429} or error.status_code >= 500)


def classify(lease: dict, snapshot, operation=None) -> RecoveryDecision:
    stop = lambda status, code=None: RecoveryDecision("stop", status, code)
    if (lease["workflow_version"] not in {"incident-v1", *ROUND_WORKFLOWS} or
            request_digest(lease["input_payload"]) != lease["input_sha256"]):
        return stop("failed", "UNSUPPORTED_OR_CORRUPT_INPUT")
    if lease["workflow_version"] in ROUND_WORKFLOWS and (
        not isinstance(lease.get("context_snapshot"), dict) or
        request_digest(lease["context_snapshot"]) != lease.get("context_sha256")
    ):
        return stop("failed", "UNSUPPORTED_OR_CORRUPT_INPUT")
    state = json_value(snapshot.values or {})
    pending = tuple(snapshot.next or ())
    tasks = tuple(snapshot.tasks or ())
    checkpoint_id = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
    if not state and not pending and not checkpoint_id and not tasks:
        if lease.get("checkpoint_started"):
            return stop("failed", "CHECKPOINT_MISSING")
        return RecoveryDecision("start")
    if state.get("incident_id") != lease["incident_id"]:
        return stop("failed", "CHECKPOINT_IDENTITY_MISMATCH")
    if state.get("request") != lease["input_payload"]:
        return stop("failed", "CHECKPOINT_INPUT_MISMATCH")
    if lease["workflow_version"] in ROUND_WORKFLOWS and (
        state.get("round_context") != lease["context_snapshot"] or state.get("run_id") != lease["run_id"]
    ):
        return stop("failed", "CHECKPOINT_INPUT_MISMATCH")
    saved = lease.get("approval_payload")
    if lease["workflow_version"] == INVESTIGATION_WORKFLOW and state.get("workflow_version") != INVESTIGATION_WORKFLOW:
        if pending != ("initialize",) or state.get("workflow_version") is not None:
            return stop("failed", "CHECKPOINT_WORKFLOW_MISMATCH")
    if saved and (saved.get("binding") != approval_binding(state) or
                  saved.get("decision", {}).get("approval_id") != (state.get("approval_request") or {}).get("approval_id")):
        return stop("failed", "APPROVAL_BINDING_MISMATCH")
    if operation and operation["state"] in {"dispatching", "outcome_unknown", "manual_required"}:
        return stop("reconciling", "OPERATION_MANUAL_REQUIRED")
    # Unjournaled legacy writes remain blocked. A durable decision only permits
    # the registered execution nodes; the executor separately checks its record.
    if (state.get("action_result") is not None or state.get("approved") is True or
            state.get("approval_status") == "approved" or
            any(name in {"execute_remediation", "verify_recovery"} for name in pending)):
        if not saved or not saved["decision"].get("approved"):
            return stop("reconciling", "WRITE_REQUIRES_RECONCILIATION")
        if (state.get("action_result") is not None or "verify_recovery" in pending) and (
                not operation or operation["state"] not in {"succeeded", "reconciled", "rejected"} or
                state.get("action_result") != operation.get("result")):
            return stop("reconciling", "OPERATION_RESULT_MISMATCH")
    phase = str(state.get("phase") or "unknown")
    if any(getattr(task, "interrupts", ()) for task in tasks):
        human_node = "await_investigation_input" if lease["workflow_version"] == INVESTIGATION_WORKFLOW else "await_user_input"
        if lease["workflow_version"] in {DIALOGUE_WORKFLOW, INVESTIGATION_WORKFLOW} and phase == "waiting_user" and pending == (human_node,):
            question = state.get("question") or {}
            answer = lease.get("answer_payload")
            # A checkpoint may already contain the previous reply and the next
            # question, even if the worker crashed before projecting that wait.
            if answer and answer in state.get("clarification_answers", []):
                answer = None
            if answer and lease["workflow_version"] == INVESTIGATION_WORKFLOW and any(
                a.get("message_id") == answer.get("message_id") for a in state.get("answers", [])
            ):
                answer = None
            if answer:
                if any(answer.get(key) != question.get(key) for key in ("question_id", "version")):
                    return stop("failed", "QUESTION_VERSION_MISMATCH")
                return RecoveryDecision("resume_user")
            return stop("waiting_user")
        if phase == "awaiting_approval" and state.get("approval_status") == "pending":
            if saved and pending == ("request_human_approval",):
                return RecoveryDecision("resume")
            return stop("waiting_approval")
        return stop("waiting_user", "NEEDS_INPUT")
    if phase == "failed" or phase.endswith("_failed"):
        return stop("failed", "WORKFLOW_FAILED")
    if not pending:
        if saved and not saved["decision"].get("approved") and phase == "approval_rejected":
            return stop("succeeded")
        if operation and operation["state"] in {"succeeded", "reconciled"} and phase == "verification_succeeded":
            return stop("succeeded")
        if phase == "remediation_skipped" or (phase == "remediation_planned" and not state.get("requires_approval")):
            return stop("succeeded")
        return stop("failed", "WORKFLOW_FAILED")
    allowed = READ_ONLY_NODES | ({"execute_remediation", "verify_recovery"} if saved and saved["decision"].get("approved") else set())
    if lease["workflow_version"] == INVESTIGATION_WORKFLOW:
        allowed = {"initialize", "decide", "collect", "await_investigation_input", "project_investigation",
                   "prepare_approval", "request_human_approval"} | (
                   {"execute_remediation", "verify_recovery"} if saved and saved["decision"].get("approved") else set())
    if not checkpoint_id or not set(pending).issubset(allowed):
        return stop("failed", "UNSUPPORTED_PENDING_TASK")
    if any(getattr(task, "name", None) not in allowed for task in tasks):
        return stop("failed", "UNSUPPORTED_PENDING_TASK")
    if any(getattr(task, "error", None) for task in tasks):
        if (lease.get("last_error") or {}).get("code") != "DEPENDENCY_TEMPORARY":
            return stop("failed", "PENDING_TASK_FAILED")
    return RecoveryDecision("continue")
