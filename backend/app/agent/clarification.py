"""Finite human-information questions; tool evidence must still come from collection."""
import re
import json
from hashlib import sha256
from datetime import UTC, datetime

from langgraph.types import interrupt

QUESTION_SLOTS = (
    ("onset", r"开始时间|发生时间|出现时间|何时|起始|onset|start time|when", "异常大约从什么时候开始？不知道可以跳过。"),
    ("changes", r"发布|变更|人工操作|recent change|release|manual action", "异常前后做过哪些发布、配置变更或人工操作？"),
    ("symptom", r"具体报错|报错内容|用户症状|错误示例|user symptom|error example", "用户实际看到什么报错或异常？请提供不含敏感信息的示例。"),
    ("impact", r"影响范围|受影响|复现条件|impact|reproduction", "哪些请求或用户受影响，在什么条件下可以复现？"),
)


def prepare_clarification(state):
    diagnosis = state.get("diagnosis") or {}
    asked = set(state.get("asked_slots", []))
    count = state.get("clarification_round", 0)
    missing = (diagnosis.get("assessment") or {}).get("missing_evidence", [])
    text = "\n".join(missing)
    slots = [{"slot": slot, "text": question} for slot, pattern, question in QUESTION_SLOTS
             if slot not in asked and re.search(pattern, text, re.I)]
    if state.get("phase") != "diagnosis_completed" or diagnosis.get("fault_category") != "unknown" or not slots or count >= 2:
        return {"question": None, "clarification_exhausted": count >= 2 and diagnosis.get("fault_category") == "unknown"}
    version = count + 1
    from backend.app.runtime.budget import CURRENT, BudgetExceeded
    budget = CURRENT.get()
    if budget:
        try:
            budget.decision(f"question:{version}")
        except BudgetExceeded:
            return {"question": None, "clarification_exhausted": True,
                      "trace": [{"step": "budget", "status": "failed", "timestamp": datetime.now(UTC).isoformat(),
                                 "message": "追问预算已用完；保留未知结论并交接人工。"}]}
    question = {"question_id": "q-" + sha256(f'{state["run_id"]}:{version}'.encode()).hexdigest()[:24],
                "version": version, "questions": slots[:2], "reason": "诊断缺少只能由人工补充的信息；回答仍需采集证据核实。",
                "evidence_revision": sha256(json.dumps(state.get("evidence", []), sort_keys=True, default=str).encode()).hexdigest(),
                "evidence_ids": [e["evidence_id"] for e in state.get("evidence", []) if e.get("evidence_id")]}
    return {"question": question, "phase": "waiting_user", "clarification_round": version,
            "asked_slots": [*state.get("asked_slots", []), *(q["slot"] for q in question["questions"])]}


def await_user_input(state):
    question = state["question"]
    answer = interrupt(question)
    if answer.get("question_id") != question["question_id"] or answer.get("version") != question["version"]:
        raise ValueError("STALE_QUESTION_ANSWER")
    claims = [*state.get("clarification_answers", []), answer]
    return {"question": None, "clarification_answers": claims,
            "phase": "clarification_skipped" if answer["skip"] else "clarification_received"}


def wire_clarification(builder, route_after_diagnosis):
    builder.add_node("prepare_clarification", prepare_clarification)
    builder.add_node("await_user_input", await_user_input)
    builder.add_edge("diagnose_incident", "prepare_clarification")
    builder.add_conditional_edges("prepare_clarification",
        lambda state: "ask" if state.get("question") else route_after_diagnosis(state),
        {"ask": "await_user_input", "plan": "plan_remediation", "skip": "skip_remediation", "stop": "__end__"})
    builder.add_conditional_edges("await_user_input",
        lambda state: "skip" if state["phase"] == "clarification_skipped" else "collect",
        {"skip": "skip_remediation", "collect": "collect_evidence"})
