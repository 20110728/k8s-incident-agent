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


class RemediationPlan(BaseModel):
    action: str
    parameters: dict[str, Any]
    risk_level: Literal["low", "medium", "high"]
    expected_result: str
    rollback_plan: str
