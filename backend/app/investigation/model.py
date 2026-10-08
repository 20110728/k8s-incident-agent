"""One provider call per attempt; result and usage saved before graph advancement."""
import time
from backend.app.investigation.context import SYSTEM, CALL_SECONDS, CALL_TOKENS, OUTPUT_LIMIT, encode, estimate, INPUT_LIMIT
from backend.app.investigation.contracts import Decision
from backend.app.investigation.records import digest, saved_result


class InvestigationModel:
    def __init__(self):
        from backend.app.llm.client import build_chat_model
        from backend.app.rag.settings import get_rag_settings
        settings = get_rag_settings().model_copy(update={"llm_timeout_seconds": CALL_SECONDS, "llm_max_retries": 0})
        model = build_chat_model(settings)
        model.max_tokens = OUTPUT_LIMIT
        self.runnable = model.with_structured_output(Decision, method="json_schema", strict=True, include_raw=True)

    def invoke(self, prompt):
        response = self.runnable.invoke([("system", SYSTEM), ("human", encode(prompt))])
        parsed = response.get("parsed")
        return {"parsed": parsed.model_dump(mode="json") if isinstance(parsed, Decision) else parsed,
                "parse_error": "STRUCTURED_OUTPUT_INVALID" if response.get("parsing_error") else None,
                "usage": getattr(response.get("raw"), "usage_metadata", None) or {}}


def call_model(model, budget, prompt, request_id, *, decision_key=None, terminal=False):
    if estimate(prompt) > INPUT_LIMIT:
        budget.deny("INVESTIGATION_CONTEXT_TOO_LARGE")
    ticket, fresh = budget.reserve("investigation_model", CALL_SECONDS, tokens=CALL_TOKENS,
        request_id=request_id, fingerprint=digest(prompt), decision_key=decision_key,
        keep_tokens=0 if terminal else CALL_TOKENS, keep_seconds=0 if terminal else CALL_SECONDS,
        metadata={"purpose": "terminal" if terminal else "decision", "input_estimate": estimate(prompt),
                  "input_limit": INPUT_LIMIT, "output_limit": OUTPUT_LIMIT, "estimate_source": "utf8_bytes_div_3_plus_schema_and_512"})
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
        # Normalize provider values into bounded serializable data. Do not store
        # arbitrary raw provider objects or another copy of the full prompt.
        if len(encode(result)) > 24000:
            result = {"parsed": None, "parse_error": "MODEL_RESULT_TOO_LARGE"}
    except Exception:
        result, usage, actual = {"error": "MODEL_REQUEST_FAILED"}, {}, None
    budget.settle(ticket, time.monotonic() - start, tokens=actual, usage=usage or None,
                  status="failed_or_unknown" if result.get("error") else "completed", result=result)
    return result
