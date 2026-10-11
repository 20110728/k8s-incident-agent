"""Saved last-call inputs, provider failures and read-only export; real budget storage."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest

from backend.app.investigation.context import build_context
from backend.app.investigation.exchange import last_exchange_report, final_response
from backend.app.investigation.model import call_model
from backend.app.investigation.brief_debug import brief_debug_report
from backend.app.runtime.budget import budget_view
from backend.app.persistence.rounds import RoundRepository
from backend.tests.investigation_loop.test_loop import case, storage, state, identified, toolbox, Model, stop


@pytest.mark.parametrize("failure", [False, True])
def test_last_exchange_persists_exact_messages_and_output_or_provider_error(case, failure):
    budget, box, _ = case
    first_prompt = build_context(box.state, box.manifest(), [])
    first = call_model(Model(stop), budget, first_prompt, "6b2:decision:1")
    prompt = build_context(box.state, box.manifest(), [], terminal_only=True)
    model = Mock()
    if failure:
        error = RuntimeError("provider rejected request")
        error.status_code = 400
        error.request_id = "provider-id"
        error.body = {"error": {"code": "context_length_exceeded", "message": "input too long; token=private-value"}}
        model.invoke.side_effect = error
    else:
        model.invoke.return_value = {"parsed": {"decision": stop(prompt)}, "usage": {"total_tokens": 123},
                                    "final_output": {"content": "final answer", "tool_calls": []}}
    call_model(model, budget, prompt, "6b2:final:2", terminal=True)
    row = {"incident_id": budget.lease["incident_id"], "run_id": budget.lease["run_id"]}
    with budget.edit() as data:
        saved = deepcopy(data)
    report = last_exchange_report(row, saved)
    assert report["request_id"] == "6b2:final:2" and report["input_available"]
    assert json.loads(report["input"]["messages"][1]["content"]) == prompt
    assert report["input"]["response_schema"]
    if failure:
        assert report["output"] is None and report["diagnostics"]["http_status"] == 400
        brief = brief_debug_report(row, saved)
        attempt = next(c for c in brief["model_attempts"] if c["id"] == "6b2:final:2")
        assert attempt["provider_code"] == "context_length_exceeded"
        assert attempt["error_type"] == "RuntimeError" and attempt["provider_request_id"] == "provider-id"
        assert "private-value" not in json.dumps([report, brief])
    else:
        assert report["output"]["content"] == "final answer" and report["parsed_output"]
    # Replaying an earlier request must not replace the last captured input.
    assert call_model(Model(lambda _: pytest.fail("paid replay")), budget, first_prompt, "6b2:decision:1") == first
    with budget.edit() as data:
        assert last_exchange_report(row, data) == report
    public = budget_view(RoundRepository(budget.repo._connect), row["incident_id"], row["run_id"])
    assert "response_schema" not in json.dumps(public) and "final answer" not in json.dumps(public)


def test_legacy_last_call_order_and_missing_input_are_explicit():
    data = {"calls": {
        "a": {"kind": "investigation_model", "request_id": "6b2:decision:5:correction", "status": "completed",
              "result": {"parsed": {"decision": {"action": "stop"}}}},
        "b": {"kind": "investigation_model", "request_id": "6b2:decision:5", "status": "completed"},
        "c": {"kind": "investigation_model", "request_id": "6b2:decision:1", "status": "completed"},
    }}
    report = last_exchange_report({"incident_id": "i", "run_id": "r"}, data)
    assert report["request_id"].endswith(":correction")
    assert report["input"] is None and not report["input_available"]
    assert report["parsed_output"]["decision"]["action"] == "stop"


def test_final_response_preserves_answer_not_reasoning_or_sdk_objects():
    raw = SimpleNamespace(content=[{"type": "reasoning", "text": "hidden"},
        {"type": "text", "text": "answer"}], tool_calls=[{"name": "Decision", "args": {"token": "private"}}],
        additional_kwargs={"reasoning_content": "hidden"})
    result = final_response(raw)
    assert result["content"] == [{"type": "text", "text": "answer"}]
    assert "hidden" not in json.dumps(result) and "private" not in json.dumps(result)
