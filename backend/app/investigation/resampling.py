"""Server-issued sampling generations; model reasons alone never permit a repeat."""
from copy import deepcopy
from datetime import UTC, datetime

from backend.app.investigation.records import digest, IncompleteRequest

FRESHNESS_SECONDS = {"resource_summary": 30, "registered_business": 30, "endpoint_slice": 30,
                     "pod_logs": 60, "pod_events": 60, "deployment": 60, "replica_set": 60}


def authorize_sample(budget, toolbox, request, request_id, reason, answers):
    semantic_key = toolbox.query_key(request)
    fingerprint = digest([request, reason, [a["message_id"] for a in answers]])
    with budget.edit() as data:
        grants = data.setdefault("sampling_grants", {})
        saved = grants.get(request_id)
        if saved:
            if saved["fingerprint"] != fingerprint:
                raise ValueError("SAMPLING_INPUT_CHANGED")
            return deepcopy(saved["grant"])
        prior = [c for c in data["calls"].values() if c["kind"] == "tool" and
                 c.get("metadata", {}).get("semantic_key") == semantic_key]
        generation, basis = 0, "initial"
        if prior:
            last = max(prior, key=lambda c: c["metadata"].get("sample_generation", 0))
            result = last.get("result")
            if not result:
                raise IncompleteRequest("REQUEST_OUTCOME_UNKNOWN")
            if result.get("error_code") or result.get("coverage") == "unknown":
                raise ValueError("FAILED_SAMPLE_REQUIRES_NEW_RUN")
            if request.get("previous"):
                raise ValueError("HISTORICAL_LOG_RESAMPLE_NOT_ALLOWED")
            collected = datetime.fromisoformat(result["collected_at"])
            generation = last["metadata"].get("sample_generation", 0) + 1
            if reason == "user_change":
                # Only consume a receipt already accepted by the human boundary.
                receipts = data.get("investigation_answers", {})
                candidates = [a for a in answers if a.get("slot") == "changes" and not a["skip"] and
                              request["resource_ref"] in a["changed_resource_refs"] and
                              datetime.fromisoformat(a["accepted_at"]) > collected and
                              receipts.get(a["question_id"], {}).get("answer") == a]
                if not candidates:
                    raise ValueError("NO_CONFIRMED_CHANGE_FOR_QUERY")
                basis = "change:" + candidates[-1]["message_id"]
            elif reason == "stale":
                age = (datetime.now(UTC) - collected).total_seconds()
                if age < FRESHNESS_SECONDS[request["tool"]]:
                    raise ValueError("SAMPLE_STILL_FRESH")
                basis = "stale:" + last["request_id"]
            else:
                raise ValueError("RESAMPLE_REASON_REQUIRED")
            if any(g["grant"]["semantic_key"] == semantic_key and g["grant"]["basis"] == basis for g in grants.values()):
                raise ValueError("RESAMPLE_BASIS_ALREADY_USED")
        elif reason is not None:
            raise ValueError("RESAMPLE_REQUIRES_PRIOR_SAMPLE")
        grant = {"semantic_key": semantic_key, "generation": generation, "basis": basis,
                 "query_key": semantic_key if generation == 0 else digest([semantic_key, generation])}
        grants[request_id] = {"fingerprint": fingerprint, "grant": grant}
        return deepcopy(grant)
