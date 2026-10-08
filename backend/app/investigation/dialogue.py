"""Internal human resume protocol. Claims never become Kubernetes observations."""
from copy import deepcopy
from datetime import UTC, datetime
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.investigation.records import digest

VERSION = "interactive-investigation-v2"


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question_id: str
    version: int = Field(ge=1, le=2)
    message_id: str = Field(min_length=1, max_length=100)
    text: str = Field(default="", max_length=2000)
    skip: bool = False
    # Explicit user input; never extracted by an LLM from free-form prose.
    changed_resource_refs: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def meaningful(self):
        if self.skip and (self.text.strip() or self.changed_resource_refs):
            raise ValueError("SKIPPED_ANSWER_MUST_BE_EMPTY")
        if not self.skip and not self.text.strip():
            raise ValueError("ANSWER_TEXT_REQUIRED")
        if len(self.changed_resource_refs) != len(set(self.changed_resource_refs)):
            raise ValueError("DUPLICATE_CHANGED_RESOURCE")
        return self


def question_for(state, decision, budget, manifest, evidence):
    slot = decision.get("slot")
    if slot not in {"onset", "changes", "symptom", "impact"}:
        raise ValueError("HUMAN_INFORMATION_SLOT_REQUIRED")
    if slot in state.get("asked_slots", []):
        raise ValueError("QUESTION_SLOT_ALREADY_ASKED")
    count = len(state.get("asked_slots", [])) + 1
    if count > 2:
        raise ValueError("HUMAN_QUESTION_LIMIT")
    return {"question_id": "q-" + digest([VERSION, budget.lease["run_id"], count])[:24],
            "version": count, "run_id": budget.lease["run_id"], "source": "model_generated",
            "questions": [{"slot": slot, "text": decision["question"]}], "reason": decision["reason"],
            "evidence_ids": decision["evidence_ids"], "evidence_revision": digest(evidence),
            "change_candidates": manifest["resources"] if slot == "changes" else []}


def accept_answer(budget, question, value):
    if question["run_id"] != budget.lease["run_id"]:
        raise ValueError("QUESTION_RUN_MISMATCH")
    answer = Answer.model_validate(value)
    if answer.question_id != question["question_id"] or answer.version != question["version"]:
        raise ValueError("STALE_QUESTION_ANSWER")
    allowed = {r["resource_ref"] for r in question["change_candidates"]}
    if not set(answer.changed_resource_refs) <= allowed:
        raise ValueError("ANSWER_RESOURCE_NOT_ALLOWED")
    payload = answer.model_dump(mode="json")
    fingerprint = digest(payload)
    with budget.edit() as data:
        receipts = data.setdefault("investigation_answers", {})
        old = receipts.get(answer.question_id)
        if old:
            if old["fingerprint"] != fingerprint:
                raise ValueError("QUESTION_ALREADY_ANSWERED")
            return deepcopy(old["answer"])
        if any(r["answer"]["message_id"] == answer.message_id for r in receipts.values()):
            raise ValueError("ANSWER_MESSAGE_ALREADY_USED")
        saved = {**payload, "accepted_at": datetime.now(UTC).isoformat(),
                 "slot": question["questions"][0]["slot"], "source": "user_supplied_unverified",
                 "evidence_revision": question["evidence_revision"]}
        receipts[answer.question_id] = {"fingerprint": fingerprint, "answer": saved}
        return deepcopy(saved)


def advance_interactive(graph, budget, config, baseline, *, answer=None):
    """Validate input BEFORE Command(resume=...). Caller owns lease/heartbeat.

    This internal boundary is not an HTTP endpoint or an authentication layer.
    A malformed/stale answer must never enter LangGraph's saved resume values.
    """
    from langgraph.types import Command
    snapshot = graph.get_state(config)
    if snapshot.values:
        version = snapshot.values.get("workflow_version")
        initializing = snapshot.next == ("initialize",) and snapshot.values.get("baseline") == baseline
        if version != VERSION and not initializing:
            raise ValueError("INVESTIGATION_WORKFLOW_VERSION_CHANGED")
        if digest(snapshot.values.get("baseline")) != digest(baseline):
            raise ValueError("INVESTIGATION_BASELINE_CHANGED")
    waiting = snapshot.next == ("await_investigation_input",)
    if answer is not None:
        parsed = Answer.model_validate(answer)
        with budget.edit() as data:
            receipt = data.get("investigation_answers", {}).get(parsed.question_id)
            if receipt:
                if receipt["fingerprint"] != digest(parsed.model_dump(mode="json")):
                    raise ValueError("QUESTION_ALREADY_ANSWERED")
                if any(a["message_id"] == parsed.message_id for a in snapshot.values.get("answers", [])):
                    return snapshot.values
        if not waiting:
            raise ValueError("NO_PENDING_INVESTIGATION_QUESTION")
        accept_answer(budget, snapshot.values["question"], answer)
        value = Command(resume=answer)
    elif waiting or snapshot.values and not snapshot.next:
        return snapshot.values
    else:
        value = None if snapshot.values else {"baseline": baseline}
    with budget.activity():
        return graph.invoke(value, config)
