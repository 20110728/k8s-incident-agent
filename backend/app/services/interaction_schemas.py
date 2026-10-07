"""4B-1 commands: an interaction can propose an action, never authorize a write."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.persistence.runs import run_summary
from backend.app.services.message_schemas import CreateMessage

INTERACTION_WORKFLOW = "interaction-v1"
Intent = Literal["status", "explain", "compare", "supplement", "investigate", "recheck", "approval", "clarify"]


class CreateInteraction(CreateMessage):
    intent: Literal["auto", "status", "explain", "compare", "supplement", "investigate", "recheck", "stop"] = "auto"
    reference_run_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9-]+$")
    compare_run_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9-]+$")

    @model_validator(mode="after")
    def valid_references(self):
        if self.intent not in {"explain", "compare"} and (self.reference_run_id or self.compare_run_id):
            raise ValueError("Historical references are only supported by explain/compare")
        if self.compare_run_id and self.intent != "compare":
            raise ValueError("Second reference requires compare")
        return self


class IntentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Intent
    reason: str = Field(min_length=1, max_length=600)


class Explanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=2400)
    citation_ids: list[str] = Field(max_length=20)
    unknowns: list[str] = Field(max_length=10)


def interaction_view(row):
    return {"run": run_summary(row), "output": row["output_snapshot"],
            "source_message_id": row["context_snapshot"]["message_id"],
            "calls": row["interaction_progress"].get("calls", [])}
