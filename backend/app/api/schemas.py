from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.agent.schemas import (
    ActionExecutionResult,
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


class IncidentStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

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

    retrieval_query: str | None = None
    retrieved_runbooks: list[dict[str, Any]] = Field(
        default_factory=list,
    )

    diagnosis: Diagnosis | None = None
    llm_model: str | None = None
    llm_usage: dict[str, int] = Field(
        default_factory=dict,
    )
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
            retrieval_query=state.get(
                "retrieval_query"
            ),
            retrieved_runbooks=list(
                state.get("retrieved_runbooks")
                or []
            ),
            diagnosis=state.get("diagnosis"),
            llm_model=state.get("llm_model"),
            llm_usage=dict(
                state.get("llm_usage") or {}
            ),
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