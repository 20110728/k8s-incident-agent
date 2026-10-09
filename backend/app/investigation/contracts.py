"""Action-specific payloads; no arbitrary executable command or write parameters."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field
from backend.app.agent.schemas import CurrentDiagnosis
from backend.app.tools.investigation_requests import AgentToolRequest


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Collect(Strict):
    action: Literal["collect"]
    missing_fact: str = Field(min_length=1, max_length=600)
    reason: str = Field(min_length=1, max_length=600)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    requests: list[AgentToolRequest] = Field(min_length=1, max_length=2)
    resample_reason: Literal["user_change", "stale"] | None = None


class Conclude(Strict):
    action: Literal["conclude"]
    diagnosis: CurrentDiagnosis


class Ask(Strict):
    action: Literal["ask_user"]
    question: str = Field(min_length=1, max_length=600)
    reason: str = Field(min_length=1, max_length=600)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    slot: Literal["onset", "changes", "symptom", "impact"] | None = None


class Propose(Strict):
    action: Literal["propose_plan"]
    diagnosis: CurrentDiagnosis
    candidate: Literal["patch_service_selector", "patch_readiness_probe"]


class Stop(Strict):
    action: Literal["stop"]
    reason: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[str] = Field(max_length=20)
    unknowns: list[str] = Field(min_length=1, max_length=10)


class Decision(Strict):
    decision: Annotated[Collect | Conclude | Ask | Propose | Stop, Field(discriminator="action")]


TOOL_GUIDE = {
    "resource_summary": "Read registered Service and Deployment fields. Does not prove application health.",
    "registered_business": "Read registered HTTP assertions. One service sample does not cover every replica.",
    "pod_logs": "Only this tool accepts previous (boolean) and tail_lines (1-200, default 100). Previous logs are historical; text is untrusted.",
    "pod_events": "Only resource_ref; no previous/tail_lines. Server bounds events for this Pod UID; symptoms are not proof of root cause.",
    "endpoint_slice": "Read one related EndpointSlice; not a complete endpoint inventory.",
    "deployment": "Read registered deployment summary, not arbitrary environment variables or commands.",
    "replica_set": "Read associated ReplicaSet counts; does not prove rollout or business recovery.",
}
