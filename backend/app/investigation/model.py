"""One provider call per attempt; result and usage saved before graph advancement."""
import time
from backend.app.investigation.context import SYSTEM, CALL_SECONDS, CALL_TOKENS, OUTPUT_LIMIT, encode, estimate
from backend.app.investigation.contracts import Decision
from backend.app.investigation.records import digest, saved_result
from backend.app.investigation.diagnostics import provider_diagnostics


class InvestigationModel:
    def __init__(self):
        from backend.app.llm.client import build_chat_model
        from backend.app.rag.settings import get_rag_settings
        settings = get_rag_settings().model_copy(update={"llm_max_retries": 0})
        model = build_chat_model(settings)
        model.max_tokens = None
        self.runnable = model.with_structured_output(Decision, method="json_schema", strict=True, include_raw=True)

    def invoke(self, prompt):
        response = self.runnable.invoke([("system", SYSTEM), ("human", encode(prompt))])
        parsed = response.get("parsed")
        return {"parsed": parsed.model_dump(mode="json") if isinstance(parsed, Decision) else parsed,
                "parse_error": "STRUCTURED_OUTPUT_INVALID" if response.get("parsing_error") else None,
                "diagnostics": provider_diagnostics(response),
                "usage": getattr(response.get("raw"), "usage_metadata", None) or {}}


def terminal_mode(budget, request_id, forced=False):
    """Persist routing before invocation so replay never changes a paid prompt."""
    with budget.edit() as data:
        modes = data.setdefault("investigation_model_modes", {})
        if request_id in modes:
            return modes[request_id]
        previous = data.get("requests", {}).get(request_id)
        if previous:
            mode = data["calls"][previous].get("metadata", {}).get("purpose") == "terminal"
        else:
            attempts = sum(c["kind"] == "investigation_model" for c in data["calls"].values())
            mode = forced or attempts >= data["policy"].get("model_attempts", 6) - 1
        modes[request_id] = mode
        return mode


def call_model(model, budget, prompt, request_id, *, decision_key=None, terminal=False):
    ticket, fresh = budget.reserve("investigation_model", CALL_SECONDS, tokens=CALL_TOKENS,
        request_id=request_id, fingerprint=digest(prompt), decision_key=decision_key,
        keep_tokens=0 if terminal else CALL_TOKENS, keep_seconds=0 if terminal else CALL_SECONDS,
        metadata={"purpose": "terminal" if terminal else "decision", "input_estimate": estimate(prompt),
                  "context_version": prompt.get("context_version"), "context_purpose": prompt.get("purpose"), "context_view": prompt.get("context_view"),
                  "working_state": prompt.get("working_state"),
                  "selection": [{"evidence_id": e["evidence_id"], "representation": e.get("projection"),
                                 "excerpt_digest": digest(e.get("excerpt")),
                                 "reason": "required_fact" if e["evidence_id"] in set(
                                     prompt.get("policy_facts", {}).get("business_evidence_ids", []) +
                                     prompt.get("policy_facts", {}).get("resource_evidence_ids", []) +
                                     prompt.get("policy_facts", {}).get("configuration_evidence_ids", [])) else "investigation_or_background"}
                                for e in prompt.get("evidence", [])],
                  "omission_reason": None,
                  "context_evidence": [{k: e.get(k) for k in ("evidence_id", "resource_type", "coverage", "error", "excerpt_truncated")}
                                       for e in prompt.get("evidence", [])],
                  "omitted_evidence_ids": prompt.get("omitted_evidence_ids", []),
                  "input_limit": None, "output_limit": OUTPUT_LIMIT, "estimate_source": "utf8_bytes_div_3_plus_schema_and_512"})
    if not fresh:
        return saved_result(budget, ticket)
    start = time.monotonic()
    try:
        budget.repo.assert_owned(budget.lease)
        response = model.invoke(prompt)
        usage = response.get("usage") or {}
        usage = {k: v for k, v in usage.items() if k in {"input_tokens", "output_tokens", "total_tokens"} and type(v) is int and v >= 0}
        actual = usage.get("total_tokens") or None
        result = {"parsed": response.get("parsed"), "parse_error": response.get("parse_error")}
        if response.get("diagnostics"):
            result["diagnostics"] = response["diagnostics"]
        # Store the complete parsed decision, not raw SDK objects or a second prompt.
    except Exception as error:
        result, usage, actual = {"error": "MODEL_REQUEST_FAILED", "diagnostics": {"error_type": type(error).__name__}}, {}, None
    budget.settle(ticket, time.monotonic() - start, tokens=actual, usage=usage or None,
                  status="failed_or_unknown" if result.get("error") else "completed", result=result)
    return result
