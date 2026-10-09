"""Regression for the real pod_events/tail_lines=50 failure; no live provider."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest
from pydantic import ValidationError

from backend.app.investigation.contracts import Decision
from backend.app.investigation.context import build_context
from backend.app.investigation.diagnostics import provider_diagnostics
from backend.app.investigation.model import call_model, InvestigationModel
from backend.app.investigation.records import bind_baseline, digest
from backend.app.tools.investigation_requests import ToolRequest, REQUEST_ADAPTER
from backend.tests.investigation_loop.test_loop import (
    case, storage, state, identified, toolbox, Model, run, stop, collect, tool_request,
)


def batch(prompt, *, bad=False):
    decision = collect(prompt)
    event = tool_request(prompt, tool="pod_events")
    if bad:
        event["tail_lines"] = 50
    decision["requests"].append(event)
    return decision


def test_schema_only_logs_expose_log_options():
    schema = REQUEST_ADAPTER.json_schema()
    assert set(schema["$defs"]["ResourceRequest"]["properties"]) == {"tool", "resource_ref"}
    assert {"previous", "tail_lines"} <= set(schema["$defs"]["LogRequest"]["properties"])


@pytest.mark.parametrize("addition", [{"tail_lines": 50}, {"previous": True}, {"tail_lines": "100"},
    {"previous": 0}, {"namespace": "elsewhere"}])
def test_nonlog_meaningful_or_coerced_parameters_are_rejected(addition):
    request = {"tool": "pod_events", "resource_ref": "ref-" + "a" * 24, **addition}
    with pytest.raises(ValidationError):
        ToolRequest.model_validate(request)
    with pytest.raises(ValidationError):
        Decision.model_validate({"decision": {"action": "collect", "reason": "events", "missing_fact": "probe cause",
            "evidence_ids": ["ev-1"], "requests": [request]}})


def test_valid_batch_has_no_correction_and_retains_partial_results(case):
    box = case[1]
    box.clients.core.api.list_namespaced_event.side_effect = TimeoutError("unavailable")
    model = Model(lambda p: stop(p) if p["history"] else batch(p))
    result = run(case, model)
    assert len(model.prompts) == 2  # one decision + stop, no parameter-repair call
    results = result["history"][0]["results"]
    assert results[0]["coverage"] == "partial" and results[0]["evidence_ids"]
    assert results[1]["coverage"] == "unknown" and results[1]["error_code"]
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 1


def test_real_bad_batch_corrects_once_before_any_reads_and_replays(case):
    box = case[1]
    def choose(prompt):
        if prompt["history"]:
            return stop(prompt)
        if prompt["feedback"]:
            box.clients.core.api.read_namespaced_pod_log.assert_not_called()
            box.clients.core.api.list_namespaced_event.assert_not_called()
            assert "requests.1.pod_events.tail_lines" in prompt["feedback"]
            return batch(prompt)
        return batch(prompt, bad=True)
    model = Model(choose)
    first = run(case, model)
    assert len(model.prompts) == 3 and len(first["history"][0]["results"]) == 2
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 1
    assert box.clients.core.api.list_namespaced_event.call_count == 1
    second = run(case, Model(lambda _: pytest.fail("cached run invoked model")))
    assert second == first
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 1
    assert box.clients.core.api.list_namespaced_event.call_count == 1


def test_repeated_bad_parameters_stop_after_shared_correction(case):
    model = Model(lambda p: batch(p, bad=True))
    result = run(case, model)
    assert len(model.prompts) == 2 and not result["observations"]
    assert result["output"]["stop_reason"] == "DECISION_VALIDATION_FAILED"
    assert len(result["output"]["validation_failures"]) == 2
    assert all(x["stage"] == "schema" for x in result["output"]["validation_failures"])
    case[1].clients.core.api.read_namespaced_pod_log.assert_not_called()
    case[1].clients.core.api.list_namespaced_event.assert_not_called()


@pytest.mark.parametrize("mode", ["unknown_ref", "wrong_kind", "unknown_tool"])
def test_boundary_failure_has_no_correction_even_with_bad_parameters(case, mode):
    def choose(prompt):
        decision = batch(prompt, bad=True)
        request = decision["requests"][1]
        if mode == "unknown_ref":
            request["resource_ref"] = "ref-" + "f" * 24
        elif mode == "wrong_kind":
            request["resource_ref"] = next(r["resource_ref"] for r in prompt["resources"] if r["kind"] == "service")
        else:
            request["tool"] = "exec"
        return decision
    model = Model(choose)
    result = run(case, model)
    assert len(model.prompts) == 1 and not result["observations"]
    assert result["output"]["stop_reason"] == "DECISION_VALIDATION_FAILED"
    case[1].clients.core.api.read_namespaced_pod_log.assert_not_called()


def test_old_tool_fingerprint_reuses_saved_result_with_new_short_request(case):
    budget, box, _ = case
    ref = next(key for key, value in box.refs.items() if value["kind"] == "pod")
    old = {"tool": "pod_events", "resource_ref": ref, "previous": False, "tail_lines": 100}
    short = {"tool": "pod_events", "resource_ref": ref}
    # Literal legacy field order/serialization, not generated by the new model.
    serialized = json.dumps(old, separators=(",", ":"))
    assert ToolRequest.model_validate(short).model_dump_json() == serialized
    ticket, fresh = budget.reserve("tool", 15, extra=True, key=box.query_key(old), request_id="old:tool",
        fingerprint=sha256(serialized.encode()).hexdigest())
    assert fresh
    saved = {"tool": "pod_events", "coverage": "observed", "payload": []}
    budget.settle(ticket, 1, result=saved)
    assert box.call(short, request_id="old:tool") == saved
    box.clients.core.api.list_namespaced_event.assert_not_called()


def test_changed_model_context_fails_closed_without_new_provider_call(case):
    budget, box, _ = case
    bind_baseline(budget, box.state)
    prompt = build_context(box.state, box.manifest(), [])
    old_prompt = {**deepcopy(prompt), "tool_guide": {"pod_events": "old contract"}}
    ticket, _ = budget.reserve("investigation_model", request_id="old:model", fingerprint=digest(old_prompt))
    budget.settle(ticket, 0, result={"parsed": {"decision": stop(prompt)}})
    with pytest.raises(ValueError, match="REQUEST_INPUT_CHANGED"):
        call_model(Model(lambda _: pytest.fail("upgrade resent model call")), budget, prompt, "old:model")


def test_provider_parse_error_passes_safe_field_feedback_to_correction(case):
    # Exercise include_raw parsing-error path too; a fake Model returning parsed
    # dictionaries alone would miss the production structured-output failure.
    from types import SimpleNamespace
    prompts = []
    def invoke(messages):
        prompt = json.loads(messages[1][1])
        prompts.append(prompt)
        raw = SimpleNamespace(content="", response_metadata={}, usage_metadata={"total_tokens": 120})
        if len(prompts) == 1:
            try:
                Decision.model_validate({"decision": batch(prompt, bad=True)})
            except ValidationError as error:
                return {"parsed": None, "parsing_error": error, "raw": raw}
            pytest.fail("invalid events request was accepted")
        assert "tail_lines" in prompt["feedback"]
        return {"parsed": Decision.model_validate({"decision": stop(prompt)}), "parsing_error": None, "raw": raw}
    model = InvestigationModel.__new__(InvestigationModel)
    model.runnable = SimpleNamespace(invoke=invoke)
    result = run(case, model)
    assert len(prompts) == 2 and result["output"]["status"] == "stop"
    assert not result["observations"]


@pytest.mark.parametrize("invalid", [{"tool": "exec"}, {"resource_ref": "arbitrary-pod-name"}])
def test_provider_schema_boundary_error_stops_without_correction(case, invalid):
    from types import SimpleNamespace
    calls = []
    def invoke(messages):
        prompt = json.loads(messages[1][1])
        calls.append(prompt)
        decision = collect(prompt)
        decision["requests"][0].update(invalid)
        try:
            Decision.model_validate({"decision": decision})
        except ValidationError as error:
            return {"parsed": None, "parsing_error": error, "raw": SimpleNamespace(
                content="", response_metadata={}, usage_metadata={"total_tokens": 120})}
        pytest.fail("invalid tool authority was accepted")
    model = InvestigationModel.__new__(InvestigationModel)
    model.runnable = SimpleNamespace(invoke=invoke)
    result = run(case, model)
    assert len(calls) == 1 and not result["observations"]
    assert result["output"]["validation_failures"][0]["detail"] == "TOOL_OR_RESOURCE_SCHEMA_NOT_ALLOWED"


def test_wrapped_schema_error_retains_field_path_without_exception_text():
    try:
        ToolRequest.model_validate({"tool": "pod_events", "resource_ref": "ref-" + "a" * 24, "tail_lines": 50})
    except ValidationError as error:
        wrapper = ValueError("PRIVATE provider response")
        wrapper.__cause__ = error
        diagnostics = provider_diagnostics({"parsing_error": wrapper})
        assert "tail_lines" in diagnostics["parser_detail"]
        assert "PRIVATE" not in json.dumps(diagnostics)
        assert diagnostics["tool_boundary_invalid"] is False
    else:
        pytest.fail("invalid event parameters accepted")
