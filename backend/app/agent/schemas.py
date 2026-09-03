from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

KUBERNETES_NAME_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"

FaultCategory = Literal[
    "crash_loop_backoff",
    "image_pull_backoff",
    "oom_killed",
    "readiness_probe_error",
    "service_selector_mismatch",
    "no_fault_detected",
    "unknown",
]

class IncidentRequest(BaseModel):
    namespace: str = Field(
        min_length=1,
        max_length=63,
        pattern=KUBERNETES_NAME_PATTERN,
    )
    service_name: str = Field(
        min_length=1,
        max_length=63,
        pattern=KUBERNETES_NAME_PATTERN,
    )
    description: str = Field(min_length=1, max_length=1000)

    @field_validator("namespace", "service_name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("description must not be blank")
        return stripped


class TraceEvent(BaseModel):
    step: str
    status: Literal["started", "completed", "failed"]
    message: str
    timestamp: str


class EvidenceItem(BaseModel):
    evidence_id: str
    source: str
    resource_type: str
    resource_name: str
    collected_at: str
    data: dict[str, Any]
    error: str | None = None


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fault_category: FaultCategory = Field(
        description="故障类别，只能使用预定义枚举值",
    )
    root_cause: str = Field(
        min_length=1,
        description=(
            "非空的根因结论。"
            "如果未检测到故障，必须明确说明现有证据表明服务正常；"
            "如果证据不足，必须明确说明无法确定根因以及缺少的证据。"
        ),
    )
    evidence_ids: list[str] = Field(
        min_length=1,
        description="支持结论的Evidence ID",
    )
    runbook_ids: list[str] = Field(
        description="支持结论的Runbook ID，可以为空",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="诊断置信度，范围0到1",
    )
    reasoning_summary: str = Field(
        min_length=1,
        description=(
            "非空的诊断依据摘要，必须说明关键证据如何支持诊断结论。"
        ),
    )


RemediationActionName = Literal[
    "manual_investigation",
    "patch_readiness_probe",
    "patch_service_selector",
]


class LabelPair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        min_length=1,
        max_length=63,
    )
    value: str = Field(
        min_length=1,
        max_length=63,
    )


class RemediationParameters(BaseModel):
    """
    所有动作共享的封闭参数结构。

    使用固定字段而不是任意command或patch字符串，
    防止LLM生成可以直接执行的Shell命令。
    """

    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(
        min_length=1,
        max_length=63,
        pattern=KUBERNETES_NAME_PATTERN,
    )
    resource_kind: Literal[
        "Service",
        "Deployment",
        "Pod",
    ]
    resource_name: str = Field(
        min_length=1,
        max_length=253,
    )

    # Readiness Probe动作字段
    container_name: str | None
    current_probe_path: str | None
    proposed_probe_path: str | None
    current_probe_port: str | int | None
    proposed_probe_port: str | int | None

    # Service Selector动作字段
    current_selector: list[LabelPair]
    proposed_selector: list[LabelPair]

    # 纯人工建议字段
    investigation_steps: list[str] = Field(
        max_length=5,
    )


class RemediationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: RemediationActionName
    parameters: RemediationParameters

    risk_level: Literal[
        "low",
        "medium",
        "high",
    ]

    summary: str = Field(min_length=1)
    expected_result: str = Field(min_length=1)
    rollback_plan: str = Field(min_length=1)

    evidence_ids: list[str] = Field(
        min_length=1,
    )
    runbook_ids: list[str] = Field(
        min_length=1,
    )

    requires_approval: bool


ApprovalStatus = Literal[
    "not_required",
    "pending",
    "approved",
    "rejected",
    "failed",
]


class ApprovalRequest(BaseModel):
    """An immutable approval request bound to one remediation plan."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(
        pattern=r"^apr-[0-9a-f]{16}$",
        description="Deterministic approval request ID.",
    )
    incident_id: str = Field(min_length=1)
    plan: RemediationPlan


class ApprovalDecision(BaseModel):
    """Human response used to resume an interrupted graph."""

    model_config = ConfigDict(extra="forbid", strict=True)

    approval_id: str = Field(pattern=r"^apr-[0-9a-f]{16}$")
    approved: bool
    approver: str = Field(min_length=1, max_length=100)
    comment: str = Field(default="", max_length=1000)

    @field_validator("approver")
    @classmethod
    def validate_approver(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("approver must not be blank")
        return normalized

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str) -> str:
        return value.strip()


class ApprovalRecord(BaseModel):
    """Final immutable approval audit record."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(pattern=r"^apr-[0-9a-f]{16}$")
    incident_id: str = Field(min_length=1)
    action: RemediationActionName
    approved: bool
    approver: str = Field(min_length=1, max_length=100)
    comment: str = Field(default="", max_length=1000)
    decided_at: str = Field(min_length=1)