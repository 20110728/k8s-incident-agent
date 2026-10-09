"""Bounded investigation; optional production handoff to the existing approval graph."""
from copy import deepcopy
from typing import Any

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
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
from backend.app.investigation.dialogue import VERSION, question_for, accept_answer
from backend.app.investigation.resampling import authorize_sample, FRESHNESS_SECONDS
from backend.app.agent.state import IncidentState
from backend.app.services.round_context import INVESTIGATION_WORKFLOW
from backend.app.investigation.diagnostics import record_validation, error_detail
from backend.app.tools.investigation_requests import ToolBoundaryError, ToolRequest


class InvestigationState(IncidentState, total=False):
    workflow_version: str
    baseline: dict[str, Any]
    observations: list[dict[str, Any]]
    history: list[dict[str, Any]]
    step: int
    decision: dict[str, Any]
    phase: str
    output: dict[str, Any]
    question: dict[str, Any] | None
    asked_slots: list[str]
    answers: list[dict[str, Any]]


def handoff(state, code):
    current = current_state(state["baseline"], state.get("observations", []))
    return {"status": "handoff", "stop_reason": code, "diagnosis": None,
            "policy_facts": diagnostic_facts(current),
            "known_evidence_ids": [e["evidence_id"] for e in current["evidence"]],
            "unknowns": ["调查未完成；未采集、失败或截断的证据不能证明目标健康。"],
            "next_step": "核对已保存证据；需要进一步排查时明确发起新一轮调查。",
            "cluster_writes_executed": False}


def validate_decision(value, prompt, current, toolbox, terminal):
    raw = value.get("decision") if isinstance(value, dict) else None
    if isinstance(raw, dict) and raw.get("action") == "collect" and isinstance(raw.get("requests"), list):
        visible = {r["resource_ref"] for r in prompt["resources"]}
        for request in raw["requests"]:
            toolbox.validate_boundary(request)
            if isinstance(request, dict) and isinstance(request.get("resource_ref"), str) and request["resource_ref"] not in visible:
                raise ToolBoundaryError("RESOURCE_NOT_IN_CONTEXT")
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
                raise ToolBoundaryError("RESOURCE_NOT_IN_CONTEXT")
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
    result = decision.model_dump(mode="json")
    if decision.action == "collect":
        # Preserve the old execution payload used by graph checkpoints, query
        # keys and durable tool-request fingerprints.
        result["requests"] = [ToolRequest.model_validate(r).model_dump() for r in result["requests"]]
    return result


def build_investigation_graph(budget, toolbox, model, *, checkpointer=None, interactive=False,
                              production=False, executor=None, verifier=None):
    """Pin graph shape per workflow version; production writes require saved approval."""
    if interactive and checkpointer is None:
        raise ValueError("INTERACTIVE_CHECKPOINTER_REQUIRED")
    if production and not interactive:
        raise ValueError("PRODUCTION_INVESTIGATION_REQUIRES_INTERACTIVE")
    version = INVESTIGATION_WORKFLOW if production else VERSION if interactive else "readonly-investigation-v1"
    prefix = "6b2" if interactive else "6b1"
    def initialize(state):
        baseline = toolbox.state if production else state["baseline"]
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
        bind_baseline(budget, baseline, version)
        return {"baseline": baseline, "workflow_version": version, "phase": "investigating", "step": 0, "observations": [], "history": [],
                "question": None, "answers": [], "asked_slots": []}

    def check_version(state):
        if state.get("workflow_version") != version:
            raise ValueError("INVESTIGATION_WORKFLOW_VERSION_CHANGED")

    def decide(state):
        check_version(state)
        step = state["step"] + 1
        terminal = step > 3
        request_id = f"{prefix}:{'final' if terminal else 'decision'}:{step}"
        current = current_state(state["baseline"], state["observations"])
        feedback = None
        failures = []
        try:
            for attempt in range(2):
                human = {"answers": state.get("answers", []), "asked_slots": state.get("asked_slots", []),
                         "freshness_seconds": FRESHNESS_SECONDS} if interactive else None
                prompt = build_context(current, toolbox.manifest(), state["history"], terminal_only=terminal, feedback=feedback, dialogue=human)
                if production:
                    prompt["production"] = True
                    prompt["write_limit"] = "After a human wait do not propose a write plan; start a new full investigation round first."
                response = call_model(model, budget, prompt, request_id + (":correction" if attempt else ""),
                                      decision_key=None if terminal else request_id, terminal=terminal)
                if response.get("error"):
                    return {"phase": "finished", "output": handoff(state, response["error"])}
                try:
                    if response.get("parse_error"):
                        diagnostics = response.get("diagnostics") or {}
                        if diagnostics.get("tool_boundary_invalid"):
                            raise ToolBoundaryError("TOOL_OR_RESOURCE_SCHEMA_NOT_ALLOWED")
                        detail = diagnostics.get("parser_detail")
                        raise ValueError(response["parse_error"] + (": " + detail if detail else ""))
                    decision = validate_decision(response.get("parsed"), prompt, current, toolbox, terminal)
                    if interactive and decision["action"] == "ask_user":
                        question_for(state, decision, budget, toolbox.manifest(), current["evidence"])
                    if (interactive and state.get("answers") and decision["action"] in {"conclude", "propose_plan"}
                            and decision["diagnosis"]["fault_category"] == "no_fault_detected"):
                        raise ValueError("CURRENT_HEALTH_REQUIRES_NEW_BASELINE_AFTER_HUMAN_WAIT")
                    if production and state.get("answers") and decision["action"] == "propose_plan":
                        raise ValueError("WRITE_PLAN_REQUIRES_NEW_BASELINE_AFTER_HUMAN_WAIT")
                    if not interactive and decision.get("resample_reason"):
                        raise ValueError("RESAMPLING_REQUIRES_INTERACTIVE_WORKFLOW")
                    break
                except (ValidationError, ValueError) as error:
                    # Permission/resource boundaries cannot be negotiated by retry.
                    feedback = error_detail(error)
                    failures.append(record_validation(budget, request_id + (":correction" if attempt else ""),
                        step=step, attempt=attempt, error=error, response=response, prompt=prompt))
                    if attempt or isinstance(error, ToolBoundaryError) or not correction(budget, request_id):
                        return {"phase": "finished", "output": {**handoff(state, "DECISION_VALIDATION_FAILED"),
                                                                  "validation_failures": failures}}
            record = {"step": step, "action": decision["action"], "reason": decision.get("reason"),
                      "missing_fact": decision.get("missing_fact"), "evidence_ids": decision.get("evidence_ids", [])}
            if decision["action"] == "collect":
                return {"step": step, "decision": decision, "history": [*state["history"], record], "phase": "collecting"}
            if interactive and decision["action"] == "ask_user":
                question = question_for(state, decision, budget, toolbox.manifest(), current["evidence"])
                return {"step": step, "decision": decision, "history": [*state["history"], record], "phase": "waiting_user",
                        "question": question, "asked_slots": [*state.get("asked_slots", []), decision["slot"]],
                        **({"clarification_round": question["version"]} if production else {})}
            output = {"status": decision["action"], "decision": decision, "cluster_writes_executed": False}
            if decision["action"] in {"ask_user", "propose_plan"} and not production:
                output.update(status="handoff", stop_reason="READONLY_STAGE_REQUIRES_6B2")
            return {"step": step, "decision": decision, "history": [*state["history"], record], "phase": "finished", "output": output}
        except LeaseLost:
            raise
        except (BudgetExceeded, IncompleteRequest, ValueError) as error:
            return {"phase": "finished", "output": handoff(state, str(error)[:200])}

    def collect(state):
        check_version(state)
        observations = deepcopy(state["observations"])
        results = []
        try:
            scheduled = []
            for index, request in enumerate(state["decision"]["requests"]):
                # Complete request validation occurred for the entire batch before
                # its first read. Execution rechecks identity and reserves budget.
                request_id = f"{prefix}:tool:{state['step']}:{index}"
                sampling = authorize_sample(budget, toolbox, request, request_id,
                    state["decision"].get("resample_reason"), state.get("answers", [])) if interactive else None
                scheduled.append((request, request_id, sampling))
            for request, request_id, sampling in scheduled:
                result = toolbox.call(request, request_id=request_id,
                                      keep_tokens=CALL_TOKENS, keep_seconds=CALL_SECONDS,
                                      **({"sampling": sampling} if interactive else {}))
                rows = adapt(result, request, toolbox.refs[request["resource_ref"]])
                known = {e["evidence_id"] for e in observations}
                observations.extend(e for e in rows if e["evidence_id"] not in known)
                results.append({"tool": request["tool"], "coverage": result["coverage"], "error_code": result["error_code"],
                                "evidence_ids": [e["evidence_id"] for e in rows], **({"sampling": sampling} if interactive else {})})
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

    def await_user_input(state):
        check_version(state)
        # No model, tool or budget reservation before interrupt. LangGraph
        # re-enters this node on resume, so side effects belong after interrupt.
        value = interrupt(state["question"])
        answer = accept_answer(budget, state["question"], value)
        answers = [*state.get("answers", []), answer]
        update = {"answers": answers, "question": None, "phase": "investigating"}
        if production:
            update["clarification_answers"] = answers
        if answer["skip"]:
            update.update(phase="finished", output=handoff(state, "HUMAN_QUESTION_SKIPPED"))
        return update

    def exported(node):
        def call(state):
            update = node(state)
            if production:
                merged = {**state, **update}
                if merged.get("baseline"):
                    update.update(current_state(merged["baseline"], merged.get("observations", [])))
            return update
        return call

    def project(state):
        from backend.app.investigation.production import deterministic_plan, unknown_diagnosis
        from backend.app.agent.nodes import skip_remediation, trace_event
        current = current_state(state["baseline"], state["observations"])
        output = state["output"]
        decision = output.get("decision") or {}
        diagnosis = decision.get("diagnosis") or unknown_diagnosis(current, output)
        update = {**current, "diagnosis": diagnosis, "diagnosis_model_output": decision.get("diagnosis")}
        if output["status"] == "propose_plan":
            try:
                plan = deterministic_plan({**current, "diagnosis": diagnosis}, decision["candidate"])
                update.update(phase="remediation_planned", remediation_plan=plan.model_dump(mode="json"),
                    requires_approval=True, risk_level=plan.risk_level, approved=None,
                    trace=[trace_event("plan_remediation", "completed", "白名单计划由程序生成；未调用规划模型。")])
            except ValueError as error:
                update.update(phase="remediation_failed", requires_approval=False,
                              errors=[{"stage": "plan_remediation", "code": "INVALID_DETERMINISTIC_PLAN", "message": str(error)}])
        else:
            update.update(skip_remediation({**current, "diagnosis": diagnosis}))
        return update

    end = "project_investigation" if production else END
    builder = StateGraph(InvestigationState)
    builder.add_node("initialize", exported(initialize))
    builder.add_node("decide", exported(decide))
    builder.add_node("collect", exported(collect))
    if interactive:
        builder.add_node("await_investigation_input", exported(await_user_input))
        builder.add_conditional_edges("await_investigation_input", lambda s: "decide" if s["phase"] == "investigating" else end)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "decide")
    builder.add_conditional_edges("decide", lambda s: "collect" if s["phase"] == "collecting" else
                                  "await_investigation_input" if s["phase"] == "waiting_user" else end)
    builder.add_conditional_edges("collect", lambda s: "decide" if s["phase"] == "investigating" else end)
    if production:
        from backend.app.agent.nodes import prepare_approval, request_human_approval, make_execute_remediation_node, make_verify_recovery_node
        from backend.app.agent.graph import route_after_prepare_approval, route_after_approval, route_after_execution
        builder.add_node("project_investigation", project)
        builder.add_node("prepare_approval", prepare_approval)
        builder.add_node("request_human_approval", request_human_approval)
        builder.add_node("execute_remediation", make_execute_remediation_node(executor))
        builder.add_node("verify_recovery", make_verify_recovery_node(verifier))
        builder.add_conditional_edges("project_investigation", lambda s: "prepare_approval" if s.get("requires_approval") else END)
        builder.add_conditional_edges("prepare_approval", route_after_prepare_approval, {"request": "request_human_approval", "stop": END})
        builder.add_conditional_edges("request_human_approval", route_after_approval, {"execute": "execute_remediation", "stop": END})
        builder.add_conditional_edges("execute_remediation", route_after_execution, {"verify": "verify_recovery", "stop": END})
        builder.add_edge("verify_recovery", END)
    return builder.compile(checkpointer=checkpointer)
