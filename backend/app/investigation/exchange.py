"""Read-only export of the last saved application-level investigation LLM exchange."""
import json
import re

from backend.app.tools.investigation import redact_output
from backend.app.investigation.diagnostics import safe_text


def provider_error(error):
    body = getattr(error, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else {}
    detail = detail if isinstance(detail, dict) else {}
    return {k: v for k, v in {
        "error_type": type(error).__name__,
        "http_status": getattr(error, "status_code", None),
        "provider_code": safe_text(detail["code"], 100) if detail.get("code") is not None else None,
        "provider_request_id": safe_text(getattr(error, "request_id", ""), 200) or None,
        "error_message": safe_text(detail.get("message") or str(error), 600),
    }.items() if v is not None}


def final_response(raw):
    if raw is None:
        return None
    content = getattr(raw, "content", None)
    if isinstance(content, list):
        content = [part for part in content if isinstance(part, dict) and part.get("type") in {"text", "output_text"}]
    result = {"content": content,
              "tool_calls": [{"name": c.get("name"), "args": c.get("args")}
                             for c in (getattr(raw, "tool_calls", None) or [])]}
    return json.loads(redact_output(result))


def last_exchange_report(row, data):
    calls = [(ticket, call) for ticket, call in data.get("calls", {}).items() if call.get("kind") == "investigation_model"]
    saved = data.get("last_model_input") or {}
    ticket = saved.get("ticket")
    call = data.get("calls", {}).get(ticket)
    if call is None and calls:
        # Legacy records have no input capture. JSONB dictionary order is not chronological.
        def order(pair):
            rid = pair[1].get("request_id", "")
            match = re.search(r":(?:decision|final):(\d+)", rid)
            return (int(match.group(1)) if match else -1, rid.endswith(":correction"))
        ticket, call = max(calls, key=order)
    call = call or {}
    result = call.get("result") or {}
    return json.loads(redact_output({
        "report_version": "last-llm-v1",
        "scope": "Saved investigation calls only; application messages/schema, not HTTP headers or hidden reasoning. No replay.",
        "incident_id": row["incident_id"], "run_id": row["run_id"], "request_id": call.get("request_id"),
        "status": call.get("status"), "input_available": bool(saved and saved.get("ticket") == ticket),
        "input": saved.get("input") if saved.get("ticket") == ticket else None,
        "input_saved_at": saved.get("saved_at") if saved.get("ticket") == ticket else None,
        "output": result.get("final_output"), "parsed_output": result.get("parsed"),
        "parse_error": result.get("parse_error"), "error": result.get("error"),
        "diagnostics": result.get("diagnostics"), "usage": call.get("usage"), "validation": call.get("validation"),
        "note": "Missing input/output was not captured or received; it is not reconstructed. Saved input alone does not prove provider receipt.",
    }))
