"""Classify saved state before choosing START, read-only continuation, or no execution."""
from dataclasses import dataclass
from typing import Literal

import httpx
from openai import APIConnectionError, APIStatusError
from psycopg import OperationalError

from backend.app.persistence.runs import request_digest


READ_ONLY_NODES = frozenset({
    "validate_request", "plan_collection", "collect_evidence", "retrieve_runbooks",
    "diagnose_incident", "plan_remediation", "skip_remediation", "prepare_approval",
    "request_human_approval", "finish_failure",
})


@dataclass(frozen=True)
class RecoveryDecision:
    action: Literal["start", "continue", "stop"]
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


def classify(lease: dict, snapshot) -> RecoveryDecision:
    stop = lambda status, code=None: RecoveryDecision("stop", status, code)
    if (lease["workflow_version"] != "incident-v1" or
            request_digest(lease["input_payload"]) != lease["input_sha256"]):
        return stop("failed", "UNSUPPORTED_OR_CORRUPT_INPUT")
    state = snapshot.values or {}
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
    # Until the operation ledger exists, never replay any potentially written state.
    if (state.get("action_result") is not None or state.get("approved") is True or
            state.get("approval_status") == "approved" or
            any(name in {"execute_remediation", "verify_recovery"} for name in pending)):
        return stop("reconciling", "WRITE_REQUIRES_RECONCILIATION")
    phase = str(state.get("phase") or "unknown")
    if any(getattr(task, "interrupts", ()) for task in tasks):
        if phase == "awaiting_approval" and state.get("approval_status") == "pending":
            return stop("waiting_approval")
        return stop("waiting_user", "NEEDS_INPUT")
    if phase == "failed" or phase.endswith("_failed"):
        return stop("failed", "WORKFLOW_FAILED")
    if not pending:
        if phase == "remediation_skipped" or (phase == "remediation_planned" and not state.get("requires_approval")):
            return stop("succeeded")
        return stop("failed", "WORKFLOW_FAILED")
    if not checkpoint_id or not set(pending).issubset(READ_ONLY_NODES):
        return stop("failed", "UNSUPPORTED_PENDING_TASK")
    if any(getattr(task, "name", None) not in READ_ONLY_NODES for task in tasks):
        return stop("failed", "UNSUPPORTED_PENDING_TASK")
    if any(getattr(task, "error", None) for task in tasks):
        if (lease.get("last_error") or {}).get("code") != "DEPENDENCY_TEMPORARY":
            return stop("failed", "PENDING_TASK_FAILED")
    return RecoveryDecision("continue")
