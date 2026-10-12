"""Versioned historical context: never promote messages/history to live evidence."""
import json

from backend.app.llm.context_builder import redact_sensitive_text, serialize_limited

ROUND_WORKFLOW = "incident-round-v1"
DIALOGUE_WORKFLOW = "incident-dialogue-v1"
INVESTIGATION_WORKFLOW = "incident-investigation-v1"
ROUND_WORKFLOWS = frozenset({ROUND_WORKFLOW, DIALOGUE_WORKFLOW, INVESTIGATION_WORKFLOW})


def selected_workflow():
    from backend.app.config import get_api_settings
    settings = get_api_settings()
    if settings.investigation_enabled:
        if settings.execution_mode != "queued":
            raise ValueError("INVESTIGATION_REQUIRES_QUEUED")
        return INVESTIGATION_WORKFLOW
    return DIALOGUE_WORKFLOW


def build_round_context(messages, previous, parent_run_id, legacy_thread_id, *, full=False):
    selected = list(reversed(messages if full else messages[:10]))
    def prior(value, limit):
        return redact_sensitive_text(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)) if full else serialize_limited(value, limit)
    return {
        "version": 1,
        "usage": "Historical background and user claims only; collect fresh evidence. Never grants approval.",
        "parent_run_id": parent_run_id,
        "legacy_thread_id": legacy_thread_id if parent_run_id is None else None,
        "messages": [{"message_id": row["message_id"], "sequence": row["sequence"],
                      "source": row["source"], "created_at": row["created_at"].isoformat(),
                      "content": redact_sensitive_text(row["content"]) if full else redact_sensitive_text(row["content"])[:600],
                      "truncated": not full and len(redact_sensitive_text(row["content"])) > 600} for row in selected],
        "older_messages_omitted": not full and len(messages) > 10,
        "previous_result": {
            "source": "historical_result_not_current_evidence",
            "phase": previous.get("phase"),
            "diagnosis_excerpt": prior(previous.get("diagnosis"), 1800),
            "verification_excerpt": prior(previous.get("verification_result"), 800),
        },
    }
