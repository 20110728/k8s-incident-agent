"""6B-1: collect/assess loop with terminal handoff, never approval or mutation."""
from copy import deepcopy
from typing import Any, TypedDict

from langgraph.graph import StateGraph, START, END
from pydantic import ValidationError

from backend.app.agent.diagnosis_policy import diagnostic_facts, validate_diagnosis_assessment, InvalidDiagnosisAssessment
from backend.app.agent.nodes import validate_diagnosis_references, InvalidDiagnosisReference
from backend.app.agent.remediation_policy import get_allowed_remediation_actions
from backend.app.investigation.context import build_context, CALL_TOKENS, CALL_SECONDS
from backend.app.investigation.contracts import Decision
from backend.app.investigation.evidence import current_state, adapt
from backend.app.investigation.model import call_model
from backend.app.investigation.records import bind_baseline, correction, IncompleteRequest, digest
from backend.app.runtime.budget import BudgetExceeded
from backend.app.persistence.leases import LeaseLost


class InvestigationState(TypedDict, total=False):
    workflow_version: str
    baseline: dict[str, Any]
    observations: list[dict[str, Any]]
    history: list[dict[str, Any]]
    step: int
    decision: dict[str, Any]
    phase: str
    output: dict[str, Any]


def handoff(state, code):
    current = current_state(state["baseline"], state.get("observations", []))
    return {"status": "handoff", "stop_reason": code, "diagnosis": None,
            "policy_facts": diagnostic_facts(current),
            "known_evidence_ids": [e["evidence_id"] for e in current["evidence"]],
            "unknowns": ["调查未完成；未采集、失败或截断的证据不能证明目标健康。"],
            "next_step": "核对已保存证据；需要进一步排查时明确发起新一轮调查。",
            "cluster_writes_executed": False}


def validate_decision(value, prompt, current, toolbox, terminal):
    decision = Decision.model_validate(value).decision
    if terminal and decision.action not in {"conclude", "stop"}:
        raise ValueError("TERMINAL_ONLY")
    allowed = set(prompt["available_evidence_ids"])
    refs = getattr(decision, "evidence_ids", None)
    if refs is not None and not set(refs) <= allowed:
        raise ValueError("EVIDENCE_NOT_IN_CONTEXT")
    if decision.action == "collect":
        visible = {r["resource_ref"] for r in prompt["resources"]}
        keys = []
        for request in decision.requests:
            if request.resource_ref not in visible:
                raise ValueError("RESOURCE_NOT_IN_CONTEXT")
            toolbox.validate_request(request.model_dump())
            keys.append(toolbox.query_key(request.model_dump()))
        if len(keys) != len(set(keys)):
            raise ValueError("DUPLICATE_REQUEST_IN_BATCH")
    if decision.action in {"conclude", "propose_plan"}:
        diagnosis = decision.diagnosis
        if not set(diagnosis.evidence_ids) <= allowed or not set(diagnosis.runbook_ids) <= set(prompt["available_runbook_ids"]):
            raise ValueError("DIAGNOSIS_REFERENCES_NOT_IN_CONTEXT")
        for claim in [*diagnosis.assessment.symptoms, *diagnosis.assessment.root_cause_hypotheses]:
            if not set(claim.evidence_ids) <= allowed:
                raise ValueError("CLAIM_REFERENCES_NOT_IN_CONTEXT")
        errors = []
        for validator in (lambda: validate_diagnosis_references(diagnosis=diagnosis, state=current),
                          lambda: validate_diagnosis_assessment(diagnosis, current)):
            try:
                validator()
            except (InvalidDiagnosisReference, InvalidDiagnosisAssessment) as error:
                errors.append(str(error))
        if errors:
            raise ValueError("; ".join(errors))
        if diagnosis.fault_category == "no_fault_detected" and any(e.get("error") for e in current["evidence"]):
            raise ValueError("FAILED_OBSERVATION_CANNOT_PROVE_HEALTH")
        if decision.action == "propose_plan" and decision.candidate not in get_allowed_remediation_actions({**current, "diagnosis": diagnosis.model_dump()}):
            raise ValueError("REPAIR_CANDIDATE_NOT_ALLOWED")
    return decision.model_dump(mode="json")


def build_investigation_graph(budget, toolbox, model, *, checkpointer=None):
    """Internal acceptance entry point. No production route or worker switch yet."""
    def initialize(state):
        baseline = state["baseline"]
        if digest(baseline) != digest(toolbox.state):
            raise ValueError("TOOLBOX_TARGET_MISMATCH")
        if baseline.get("incident_id") != budget.lease["incident_id"]:
            raise ValueError("RUN_INCIDENT_MISMATCH")
        ids = [e.get("evidence_id") for e in baseline.get("evidence", [])]
        if not ids or None in ids or len(ids) != len(set(ids)):
            raise ValueError("BASELINE_EVIDENCE_IDS_INVALID")
        for field in ("namespace", "service_name"):
            if baseline["request"][field] != budget.lease["input_payload"][field]:
                raise ValueError("RUN_TARGET_MISMATCH")
        bind_baseline(budget, baseline)
        return {"workflow_version": "readonly-investigation-v1", "phase": "investigating", "step": 0, "observations": [], "history": []}

    def decide(state):
        step = state["step"] + 1
        terminal = step > 3
        request_id = f"6b1:{'final' if terminal else 'decision'}:{step}"
        current = current_state(state["baseline"], state["observations"])
        feedback = None
        try:
            for attempt in range(2):
                prompt = build_context(current, toolbox.manifest(), state["history"], terminal_only=terminal, feedback=feedback)
                response = call_model(model, budget, prompt, request_id + (":correction" if attempt else ""),
                                      decision_key=None if terminal else request_id, terminal=terminal)
                if response.get("error"):
                    return {"phase": "finished", "output": handoff(state, response["error"])}
                try:
                    if response.get("parse_error"):
                        raise ValueError(response["parse_error"])
                    decision = validate_decision(response.get("parsed"), prompt, current, toolbox, terminal)
                    break
                except (ValidationError, ValueError) as error:
                    # Permission/resource boundaries cannot be negotiated by retry.
                    feedback = str(error)[:1800]
                    if attempt or "RESOURCE_" in feedback or "TOOL_RESOURCE" in feedback or not correction(budget, request_id):
                        return {"phase": "finished", "output": handoff(state, "DECISION_VALIDATION_FAILED")}
            record = {"step": step, "action": decision["action"], "reason": decision.get("reason"),
                      "missing_fact": decision.get("missing_fact"), "evidence_ids": decision.get("evidence_ids", [])}
            if decision["action"] == "collect":
                return {"step": step, "decision": decision, "history": [*state["history"], record], "phase": "collecting"}
            output = {"status": decision["action"], "decision": decision, "cluster_writes_executed": False}
            if decision["action"] in {"ask_user", "propose_plan"}:
                output.update(status="handoff", stop_reason="READONLY_STAGE_REQUIRES_6B2")
            return {"step": step, "decision": decision, "history": [*state["history"], record], "phase": "finished", "output": output}
        except LeaseLost:
            raise
        except (BudgetExceeded, IncompleteRequest, ValueError) as error:
            return {"phase": "finished", "output": handoff(state, str(error)[:200])}

    def collect(state):
        observations = deepcopy(state["observations"])
        results = []
        try:
            for index, request in enumerate(state["decision"]["requests"]):
                # Complete request validation occurred for the entire batch before
                # its first read. Execution rechecks identity and reserves budget.
                result = toolbox.call(request, request_id=f"6b1:tool:{state['step']}:{index}",
                                      keep_tokens=CALL_TOKENS, keep_seconds=CALL_SECONDS)
                rows = adapt(result, request, toolbox.refs[request["resource_ref"]])
                known = {e["evidence_id"] for e in observations}
                observations.extend(e for e in rows if e["evidence_id"] not in known)
                results.append({"tool": request["tool"], "coverage": result["coverage"], "error_code": result["error_code"],
                                "evidence_ids": [e["evidence_id"] for e in rows]})
                if result.get("target_changed"):
                    changed = {**state, "observations": observations}
                    return {"observations": observations, "phase": "finished", "output": handoff(changed, result["error_code"])}
            history = deepcopy(state["history"])
            history[-1]["results"] = results
            return {"observations": observations, "history": history, "phase": "investigating"}
        except LeaseLost:
            raise
        except (BudgetExceeded, IncompleteRequest, ValueError) as error:
            return {"observations": observations, "phase": "finished", "output": handoff({**state, "observations": observations}, str(error)[:200])}

    builder = StateGraph(InvestigationState)
    builder.add_node("initialize", initialize)
    builder.add_node("decide", decide)
    builder.add_node("collect", collect)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "decide")
    builder.add_conditional_edges("decide", lambda s: "collect" if s["phase"] == "collecting" else END)
    builder.add_conditional_edges("collect", lambda s: "decide" if s["phase"] == "investigating" else END)
    return builder.compile(checkpointer=checkpointer)
