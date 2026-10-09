from typing import Any, Literal
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from backend.app.investigation.presentation import investigation_view

from backend.app.agent.schemas import (
    ActionExecutionResult,
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalStatus,
    Diagnosis,
    IncidentRequest,
    RecoveryVerificationResult,
    RemediationPlan,
    TraceEvent,
)


class ErrorDetail(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str


class ReadinessResponse(BaseModel):
    status: Literal["ready"]
    checks: dict[str, bool]


class CreateIncidentRequest(IncidentRequest):
    model_config = ConfigDict(extra="forbid")

class SubmitApprovalRequest(ApprovalDecision):
    """Strict HTTP payload used to resume a pending approval."""

class RunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    status: Literal["queued", "running", "waiting_user", "waiting_approval", "retry_scheduled", "reconciling", "succeeded", "failed", "cancelled"]
    run_kind: Literal["diagnosis", "interaction"]
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    attempt: int = Field(ge=0)
    last_error_code: str | None
    stop_requested: bool = False
    invalidated_at: datetime | None = None
    question: dict[str, Any] | None = None
    adopted_message_ids: list[str] = Field(default_factory=list)


class IncidentStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: RunSummary | None = None
    execution_mode: Literal["sync", "queued"] = "sync"
    worker_available: bool = False
    investigation: dict[str, Any] | None = None

    incident_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    waiting_for_approval: bool

    request: dict[str, str]
    valid: bool | None = None
    error_count: int = Field(default=0, ge=0)

    collection_plan: list[str] = Field(
        default_factory=list,
    )
    evidence: list[dict[str, Any]] = Field(
        default_factory=list,
    )

    service_profile: dict[str, Any] | None = None
    retrieval_query: str | None = None
    retrieved_runbooks: list[dict[str, Any]] = Field(
        default_factory=list,
    )

    # Audit only: untrusted original model prose, not the authoritative report.
    llm_debug: dict[str, Any] = Field(default_factory=dict)
    diagnosis_model_output: dict[str, Any] | None = None
    diagnosis: Diagnosis | None = None
    llm_model: str | None = None
    llm_usage: dict[str, int] = Field(
        default_factory=dict,
    )
    clarification_exhausted: bool = False
    clarification_round: int = 0
    clarification_answers: list[dict[str, Any]] = Field(default_factory=list)
    diagnosis_retry_count: int = Field(
        default=0,
        ge=0,
    )

    remediation_plan: RemediationPlan | None = None
    risk_level: Literal[
        "low",
        "medium",
        "high",
    ] | None = None
    remediation_llm_model: str | None = None
    remediation_llm_usage: dict[str, int] = Field(
        default_factory=dict,
    )

    requires_approval: bool = False
    approved: bool | None = None
    approval_status: ApprovalStatus | None = None
    approval_request: ApprovalRequest | None = None
    approval_record: ApprovalRecord | None = None

    action_result: ActionExecutionResult | None = None
    verification_result: (
        RecoveryVerificationResult | None
    ) = None

    errors: list[dict[str, Any]] = Field(
        default_factory=list,
    )
    trace: list[TraceEvent] = Field(
        default_factory=list,
    )

    @classmethod
    def from_state(
        cls,
        *,
        incident_id: str,
        thread_id: str,
        state: dict[str, Any],
        waiting_for_approval: bool,
    ) -> "IncidentStatusResponse":
        return cls(
            investigation=investigation_view(state),
            incident_id=incident_id,
            thread_id=thread_id,
            phase=str(
                state.get("phase") or "unknown"
            ),
            waiting_for_approval=(
                waiting_for_approval
            ),
            request=dict(
                state.get("request") or {}
            ),
            valid=state.get("valid"),
            error_count=int(
                state.get("error_count") or 0
            ),
            collection_plan=list(
                state.get("collection_plan") or []
            ),
            evidence=list(
                state.get("evidence") or []
            ),
            service_profile=state.get("service_profile"),
            retrieval_query=state.get(
                "retrieval_query"
            ),
            retrieved_runbooks=list(
                state.get("retrieved_runbooks")
                or []
            ),
            llm_debug=dict(state.get("llm_debug") or {}),
            diagnosis_model_output=state.get("diagnosis_model_output"),
            diagnosis=state.get("diagnosis"),
            llm_model=state.get("llm_model"),
            llm_usage=dict(
                state.get("llm_usage") or {}
            ),
            clarification_exhausted=bool(state.get("clarification_exhausted", False)),
            clarification_round=int(state.get("clarification_round", 0)),
            clarification_answers=list(state.get("clarification_answers", [])),
            diagnosis_retry_count=int(
                state.get("diagnosis_retry_count")
                or 0
            ),
            remediation_plan=state.get(
                "remediation_plan"
            ),
            risk_level=state.get("risk_level"),
            remediation_llm_model=state.get(
                "remediation_llm_model"
            ),
            remediation_llm_usage=dict(
                state.get("remediation_llm_usage")
                or {}
            ),
            requires_approval=bool(
                state.get("requires_approval", False)
            ),
            approved=state.get("approved"),
            approval_status=state.get(
                "approval_status"
            ),
            approval_request=state.get(
                "approval_request"
            ),
            approval_record=state.get(
                "approval_record"
            ),
            action_result=state.get(
                "action_result"
            ),
            verification_result=state.get(
                "verification_result"
            ),
            errors=list(
                state.get("errors") or []
            ),
            trace=list(
                state.get("trace") or []
            ),
        )
