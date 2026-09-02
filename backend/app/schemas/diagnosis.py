from typing import Literal

from pydantic import BaseModel, Field

FaultCategory = Literal[
    "healthy",
    "service_not_found",
    "selector_mismatch",
    "image_pull_error",
    "oom_killed",
    "crash_loop",
    "readiness_probe_error",
    "unknown",
]

DiagnosisStatus = Literal[
    "healthy",
    "unhealthy",
    "unknown",
]

EvidenceSource = Literal[
    "service",
    "pod",
    "event",
    "deployment",
    "endpoint_slice",
    "collector",
]


class DiagnosisEvidenceReference(BaseModel):
    evidence_id: str
    source: EvidenceSource
    resource_name: str
    summary: str


class DiagnosisResult(BaseModel):
    namespace: str
    service_name: str
    status: DiagnosisStatus
    fault_category: FaultCategory
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[DiagnosisEvidenceReference] = Field(default_factory=list)
    recommended_action: str | None = None
    requires_approval: bool = False
