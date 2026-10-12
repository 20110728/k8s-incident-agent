"""4A-1 stores messages only; no intent routing or graph invocation."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CreateMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_message_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    content: str = Field(min_length=1, max_length=16000)

    @field_validator("content")
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("message must be nonblank and contain no NUL")
        return value  # Preserve the exact text, including whitespace.


class MessageDraft(CreateMessage):
    # Internal callers only. Public clients cannot forge role/source/evidence.
    role: Literal["user", "assistant", "tool"] = "user"
    related_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)


class MessageRecord(MessageDraft):
    adopted_by_run_ids: list[str] = Field(default_factory=list)
    message_id: str
    incident_id: str
    sequence: int
    source: Literal["user_supplied", "model_generated", "tool_observed"]
    created_at: datetime


class MessageReceipt(BaseModel):
    message: MessageRecord
    created: bool
    processing: Literal["not_started"] = "not_started"


class MessagePage(BaseModel):
    items: list[MessageRecord]
    next_before_sequence: int | None
