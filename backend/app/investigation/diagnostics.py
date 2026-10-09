"""Bounded diagnostics for failed decisions; no extra provider calls or permissions."""
from copy import deepcopy
import json
from pydantic import ValidationError
from backend.app.tools.investigation import redact_output


def safe_text(value, limit=1200):
    return json.loads(redact_output(str(value)))[:limit]


def error_detail(error):
    if isinstance(error, ValidationError):
        # Pydantic's default string includes rejected input values; omit those.
        return "; ".join(f"{'.'.join(map(str, item['loc']))}: {item['type']}"
                         for item in error.errors(include_input=False, include_context=False, include_url=False)[:10])[:1200]
    return safe_text(error)


def record_validation(budget, request_id, *, step, attempt, error, response, prompt):
    parsed = response.get("parsed") or {}
    decision = parsed.get("decision") if isinstance(parsed, dict) else None
    note = {"request_id": request_id, "step": step, "attempt": attempt + 1,
        "stage": "parse" if response.get("parse_error") else "schema" if isinstance(error, ValidationError) else "policy",
        "action": safe_text(decision.get("action"), 64) if isinstance(decision, dict) and decision.get("action") else None,
        "error_type": type(error).__name__, "detail": error_detail(error)}
    with budget.edit() as data:
        ticket = data["requests"][request_id]
        call = data["calls"][ticket]
        # Stable per request, including recovery replay. Do not grow a retry log.
        saved = call.setdefault("validation", note)
        call.setdefault("validation_context", {"evidence_ids": prompt["available_evidence_ids"],
            "runbook_ids": prompt["available_runbook_ids"],
            "resource_refs": [item["resource_ref"] for item in prompt["resources"]]})
        return deepcopy(saved)


def provider_diagnostics(response):
    raw = response.get("raw")
    metadata = getattr(raw, "response_metadata", None) or {}
    error = response.get("parsing_error")
    result = {"finish_reason": safe_text(metadata.get("finish_reason"), 80) if metadata.get("finish_reason") else None}
    if error:
        result["parser_error_type"] = type(error).__name__
        result["parser_detail"] = error_detail(error) if isinstance(error, ValidationError) else "STRUCTURED_OUTPUT_PARSE_FAILED"
        # Only final content, never reasoning_content/additional_kwargs/full prompts.
        content = getattr(raw, "content", None)
        if isinstance(content, str):
            result["output_excerpt"] = safe_text(content, 3000)
            result["output_excerpt_truncated"] = len(content) > 3000
    return result


def debug_report(row, data):
    state = row.get("output_snapshot") or {}
    calls = []
    for call in sorted(data.get("calls", {}).values(), key=lambda c: c.get("request_id", "")):
        if call["kind"] != "investigation_model":
            continue
        result = call.get("result") or {}
        calls.append({"request_id": call.get("request_id"), "status": call["status"],
            "metadata": call.get("metadata"), "usage": call.get("usage"), "elapsed_seconds": call.get("elapsed_seconds"),
            "validation": call.get("validation"), "validation_context": call.get("validation_context"),
            "result": {k: result[k] for k in ("parsed", "parse_error", "error", "diagnostics") if k in result}})
    report = {"report_version": 1, "source": "saved_records_only_no_model_or_tool_execution",
        "run": {k: row.get(k) for k in ("incident_id", "run_id", "workflow_version", "status", "created_at", "updated_at")},
        "stop_reason": (state.get("output") or {}).get("stop_reason"), "model_attempts": calls[:5],
        "available_resources": [{"resource_ref": key, **{k: value.get(k) for k in ("kind", "namespace", "name", "container")}}
            for key, value in list(data.get("references", {}).items())[:100]],
        "limits": ["Older calls may lack validation/provider details; missing data cannot be recovered by this export.",
                   "Output excerpts are redacted and bounded; omitted content is not reconstructed.",
                   "Running tasks may not yet have a published terminal snapshot."]}
    return json.loads(redact_output(report))
