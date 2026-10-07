"""Finite interaction executor; reuses leases but never resumes diagnosis/approval."""
from datetime import UTC, datetime
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

from backend.app.llm.interaction import InteractionModel, validate_answer
from backend.app.persistence.leases import LeaseLost
from backend.app.persistence.runs import request_digest
from backend.app.services.interaction_schemas import INTERACTION_WORKFLOW, IntentDecision, Explanation
from backend.app.services.recheck_service import IncidentRecheckService, RecheckRequest


def execute_interaction(repo, lease, lost, *, model_factory=InteractionModel, collector_factory=None, complete=None):
    if (lease["workflow_version"] != INTERACTION_WORKFLOW or request_digest(lease["input_payload"]) != lease["input_sha256"]
        or not isinstance(lease["context_snapshot"], dict)
        or request_digest(lease["context_snapshot"]) != lease["context_sha256"]):
        raise ValueError("INVALID_INTERACTION_INPUT")
    payload, context = lease["input_payload"], lease["context_snapshot"]
    references = context["references"]
    progress = repo.progress(lease)

    def owned():
        if lost.is_set():
            raise LeaseLost("worker lost its lease")
        repo.assert_owned(lease)

    def call(purpose):
        if purpose in progress:
            return progress[purpose]
        owned()
        if lease.get("recovery_only"):
            raise ValueError("INTERACTION_ATTEMPTS_EXHAUSTED")
        model = model_factory()
        record = {"id": str(uuid4()), "purpose": purpose, "model": model.model_name,
                  "started_at": datetime.now(UTC).isoformat(), "status": "started", "usage": None}
        progress.setdefault("calls", []).append(record)
        repo.save_progress(lease, progress)  # Crash leaves an explicit unknown-cost call.
        started = monotonic()
        try:
            parsed, usage = model.call(purpose, payload["content"], references)
            record["usage"] = usage or None
            schema = IntentDecision if purpose == "route" else Explanation
            parsed = schema.model_validate(parsed).model_dump()
            if purpose == "explain":
                validate_answer(parsed, references)
            record["status"] = "completed"
            progress[purpose] = parsed
        except Exception as error:
            record["status"] = "failed_or_unknown"
            record["error_type"] = type(error).__name__
            raise
        finally:
            record["elapsed_ms"] = round((monotonic() - started) * 1000)
            repo.save_progress(lease, progress)
        return parsed

    owned()
    decision = call("route") if payload["intent"] == "auto" else {"intent": payload["intent"], "reason": "explicit command"}
    intent = decision["intent"]
    output = {"intent": intent, "reason": decision["reason"], "cluster_writes_executed": False,
              "model_called": bool(progress.get("calls")), "fresh_observation": False}
    assistant = None
    if intent == "compare" and len(references) != 2:
        intent = "clarify"
        output.update(intent=intent, reason="请选择两个历史轮次后再比较。")
    if intent in {"explain", "compare"}:
        answer, citations = validate_answer(call("explain"), references)
        output.update(answer=answer["summary"], unknowns=answer["unknowns"], historical_only=True,
                      model_called=True, citations=citations,
                      reference_snapshots=[{key: ref[key] for key in ("run_id", "snapshot_at")} for ref in references])
        assistant = answer["summary"]
    elif intent == "status":
        output["status"] = repo.status(lease["incident_id"])
    elif intent == "supplement":
        output.update(answer="已保存补充信息，尚未核实；需要重新调查时请选择继续调查。", note_source="user_supplied_unverified")
    elif intent == "approval":
        output["answer"] = "聊天不能批准修复，请查看并使用当前方案的审批卡片。"
    elif intent == "clarify":
        output["answer"] = "请选择解释、补充信息、继续调查或重新检查。若要更换服务，请新建事件。"
    elif intent in {"recheck", "observe"}:
        result = repo.saved_recheck(lease)
        if result is None:
            if lease.get("recovery_only"):
                raise ValueError("INTERACTION_ATTEMPTS_EXHAUSTED")
            if collector_factory is None:
                from backend.app.agent.dependencies import build_kubernetes_collector
                collector_factory = lambda: build_kubernetes_collector(bounded_reads=intent == "observe")
            owned()
            collector = collector_factory()
            def collect(*args):
                owned()
                return collector.collect(*args)
            frozen = SimpleNamespace(state=references[0]["state"], waiting_for_approval=False)
            service = IncidentRecheckService(SimpleNamespace(get_incident=lambda _: frozen),
                SimpleNamespace(collect=collect), SimpleNamespace(append=lambda value: repo.save_recheck(lease, value)))
            if intent == "observe":
                from backend.app.agent.stability import observe_recheck
                observe_recheck(repo, lease, SimpleNamespace(collect=collect), references[0]["state"], payload["content"][:2000])
            else:
                service.create(lease["incident_id"], RecheckRequest(note=payload["content"][:2000]))
            result = repo.saved_recheck(lease)
        output.update(recheck=result, fresh_observation=True, note_source="user_supplied_unverified")
    elif intent == "investigate":
        output["answer"] = "已接受新的诊断轮次，将重新采集证据；本次交互没有授权修复。"
    else:
        raise ValueError("UNSUPPORTED_INTERACTION_INTENT")
    owned()
    (complete or repo.complete)(lease, output, assistant=assistant, investigate=intent == "investigate")
