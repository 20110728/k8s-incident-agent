from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

KUBERNETES_NAME_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"

FaultCategory = Literal[
    "crash_loop_backoff",
    "image_pull_backoff",
    "oom_killed",
    "readiness_probe_error",
    "service_selector_mismatch",
    "application_error",
    "dependency_error",
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


class DiagnosticFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=1200)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class RootCauseHypothesis(DiagnosticFinding):
    status: Literal["suspected", "supported"]


class DiagnosticAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["v2"]
    problem_domain: Literal[
        "deployment_configuration", "application_runtime", "dependency",
        "insufficient_evidence", "none",
    ]
    symptoms: list[DiagnosticFinding] = Field(max_length=10)
    root_cause_hypotheses: list[RootCauseHypothesis] = Field(max_length=5)
    missing_evidence: list[str] = Field(max_length=10)
    next_investigation: list[str] = Field(max_length=5)
    resource_status: Literal["ready", "not_ready", "unknown"]
    business_status: Literal["passed", "failed", "unknown"]
    unverified_scope: list[str] = Field(min_length=1, max_length=10)


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Legacy checkpoints remain readable; new LLM output requires assessment.
    assessment: DiagnosticAssessment | None = None

    fault_category: FaultCategory = Field(
        description="故障类别，只能使用预定义枚举值",
    )
    root_cause: str = Field(
        min_length=1,
        description=(
            "非空的结论摘要，明确区分已观察症状与待验证根因。"
            "如果未检测到故障，只能说明已检查范围内未见异常；"
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


class CurrentDiagnosis(Diagnosis):
    assessment: DiagnosticAssessment


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
        description="人工调查可为空；写操作必须引用检索到的Runbook",
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

ExecutionStatus = Literal[
    "succeeded",
    "already_applied",
    "conflict",
    "failed",
]


class ResourceSnapshot(BaseModel):
    """Minimal non-secret configuration snapshot."""

    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(
        min_length=1,
        max_length=63,
        pattern=KUBERNETES_NAME_PATTERN,
    )
    resource_kind: Literal[
        "Service",
        "Deployment",
    ]
    resource_name: str = Field(
        min_length=1,
        max_length=253,
    )
    resource_version: str = Field(
        min_length=1,
    )
    configuration: dict[str, Any]


class ActionExecutionResult(BaseModel):
    """Final result of one approved write operation."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(
        pattern=r"^exec-[0-9a-f]{16}$",
    )
    approval_id: str = Field(
        pattern=r"^apr-[0-9a-f]{16}$",
    )
    action: RemediationActionName
    status: ExecutionStatus

    namespace: str = Field(
        min_length=1,
        max_length=63,
        pattern=KUBERNETES_NAME_PATTERN,
    )
    resource_kind: Literal[
        "Service",
        "Deployment",
    ]
    resource_name: str = Field(
        min_length=1,
        max_length=253,
    )

    started_at: str = Field(min_length=1)
    finished_at: str = Field(min_length=1)

    before_snapshot: ResourceSnapshot | None = None
    after_snapshot: ResourceSnapshot | None = None

    applied_patch: dict[str, Any] = Field(
        default_factory=dict,
    )
    rollback_patch: dict[str, Any] = Field(
        default_factory=dict,
    )

    message: str = Field(min_length=1)
    error_code: str | None = None
    error_message: str | None = None

class ResourceMutationResult(BaseModel):
    """Result returned by one Kubernetes patch tool."""

    model_config = ConfigDict(extra="forbid")

    status: ExecutionStatus

    before_snapshot: ResourceSnapshot | None = None
    after_snapshot: ResourceSnapshot | None = None

    applied_patch: dict[str, Any] = Field(
        default_factory=dict,
    )
    rollback_patch: dict[str, Any] = Field(
        default_factory=dict,
    )

    message: str = Field(min_length=1)
    error_code: str | None = None
    error_message: str | None = None

VerificationStatus = Literal[
    "succeeded",
    "failed",
    "timeout",
    "skipped",
]


class VerificationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    passed: bool
    observed: Any
    expected: Any
    message: str = Field(min_length=1)


class RecoveryVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 旧检查点缺少这些字段时保持 resource_only，不追认业务恢复。
    verification_scope: Literal['resource_only', 'resources_and_registered_business'] = 'resource_only'
    resource_verification_status: VerificationStatus | None = None
    resource_status: Literal['ready', 'not_ready', 'unknown'] = 'unknown'
    business_status: Literal['passed', 'failed', 'unknown', 'skipped'] = 'skipped'
    post_repair_evidence: list[dict[str, Any]] = Field(default_factory=list)
    post_repair_profile: dict[str, Any] | None = None
    post_repair_collection_errors: list[dict[str, Any]] = Field(default_factory=list)
    unverified_scope: list[str] = Field(default_factory=lambda: ['业务恢复未验证（旧版资源验证）'])

    execution_id: str = Field(
        pattern=r"^exec-[0-9a-f]{16}$",
    )
    action: RemediationActionName
    status: VerificationStatus

    started_at: str = Field(min_length=1)
    finished_at: str = Field(min_length=1)
    attempts: int = Field(ge=0)

    checks: list[VerificationCheck] = Field(
        default_factory=list,
    )

    desired_replicas: int | None = None
    available_replicas: int | None = None
    ready_pods: int | None = None
    ready_endpoints: int | None = None

    message: str = Field(min_length=1)
    error_code: str | None = None
    error_message: str | None = None