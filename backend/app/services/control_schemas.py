from typing import Literal
from pydantic import Field, model_validator
from backend.app.services.message_schemas import CreateMessage


class ControlRequest(CreateMessage):
    action: Literal["stop", "supplement", "investigate"]


class AnswerQuestion(CreateMessage):
    content: str = Field(min_length=1, max_length=6000)
    question_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1, le=2)
    answers: dict[str, str] = Field(default_factory=dict, max_length=2)
    skip: bool = False
    changed_resource_refs: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def bounded_answer(self):
        if (self.skip and (self.answers or self.changed_resource_refs)) or (not self.skip and not self.answers):
            raise ValueError("Supply answers or skip, not both")
        if any(not value.strip() or len(value) > 2000 or "\x00" in value for value in self.answers.values()):
            raise ValueError("Answers must be nonblank and at most 2000 characters")
        if len(self.changed_resource_refs) != len(set(self.changed_resource_refs)):
            raise ValueError("Duplicate changed resource reference")
        return self
