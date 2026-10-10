"""Read-only, bounded public projection; never expose prompts or provider payloads."""
import json
from backend.app.tools.investigation import redact_output
from backend.app.services.round_context import INVESTIGATION_WORKFLOW


def text(value, limit=800):
    return json.loads(redact_output(str(value)))[:limit] if value is not None else None


def ids(values):
    return [text(value, 128) for value in (values or [])[:32]]


def investigation_view(state):
    if state.get("workflow_version") != INVESTIGATION_WORKFLOW:
        return None
    steps = []
    for entry in state.get("history", []):
        steps.append({"step": entry["step"], "action": text(entry.get("action"), 64),
            "reason": text(entry.get("reason")), "missing_fact": text(entry.get("missing_fact")),
            "evidence_ids": ids(entry.get("evidence_ids")),
            "results": [{"tool": text(result.get("tool"), 64), "coverage": text(result.get("coverage"), 64),
                "error_code": text(result.get("error_code")), "evidence_ids": ids(result.get("evidence_ids")),
                "generation": (result.get("sampling") or {}).get("generation", 0)}
                for result in entry.get("results", [])]})
    active = {item["evidence_id"] for item in state.get("evidence", [])}
    baseline = (state.get("baseline") or {}).get("evidence", [])
    rows = [(item, "baseline") for item in baseline] + [(item, "additional") for item in state.get("observations", [])]
    observations = [{"evidence_id": text(item["evidence_id"], 128), "origin": origin,
        "resource_type": text(item.get("resource_type"), 64), "resource_name": text(item.get("resource_name"), 253),
        "collected_at": text(item.get("collected_at"), 64), "coverage": text(item.get("coverage", "baseline_snapshot"), 64),
        "error": text(item.get("error")), "truncated": bool(item.get("truncated")),
        "current": item["evidence_id"] in active} for item, origin in rows[:64]]
    output = state.get("output") or {}
    return {"version": 1, "steps": steps, "observations": observations,
        "validation_failures": [{"attempt": item.get("attempt"), "stage": text(item.get("stage"), 32),
            "action": text(item.get("action"), 64), "detail": text(item.get("detail"), 1200)}
            for item in output.get("validation_failures", [])[:2]],
        "omitted_observations": max(0, len(rows) - 64), "outcome": text(output.get("status"), 64),
        "stop_reason": text(output.get("stop_reason") or (output.get("decision") or {}).get("reason")),
        "question_count": len(state.get("asked_slots", [])), "answer_count": len(state.get("answers", []))}
