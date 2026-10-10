"""HTTP boundary and complete three-round collection, including correction/replay."""
from io import BytesIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client
from urllib3.response import HTTPResponse

from backend.app.tools.log_text import read_log_response
from backend.app.investigation.cards import evidence_card
from backend.app.investigation.context import estimate, INPUT_LIMIT
from backend.tests.investigation_loop.test_loop import (
    case, storage, state, identified, toolbox, Model, stop,
)
from backend.tests.investigation_loop.test_c2_fix import rich_baseline
from backend.tests.investigation_dialogue.test_dialogue import session


def response():
    return HTTPResponse(body=BytesIO(b'2026-10-10T07:35:00Z readiness 503\n' * 100),
                        status=200, preload_content=False)


def test_real_sdk_raw_response_bypasses_string_deserialization(monkeypatch):
    api = client.ApiClient()
    transport = Mock(return_value=response())
    monkeypatch.setattr(api, "request", transport)
    try:
        raw = client.CoreV1Api(api).read_namespaced_pod_log(
            name="pod", namespace="agent-demo", _preload_content=False)
        text = read_log_response(raw)
        assert text.count("\n") == 100 and not text.startswith("b'")
        assert transport.call_args.kwargs["_preload_content"] is False
        assert raw.closed
    finally:
        api.close()


def test_bad_raw_response_closes_connection():
    raw = Mock()
    raw.read.return_value = b'\xff'
    with pytest.raises(UnicodeDecodeError):
        read_log_response(raw)
    raw.close.assert_called_once()
    raw.release_conn.assert_called_once()


def test_three_rounds_six_samples_correction_and_final_replay(case):
    budget, box, _ = case
    rich_baseline(box.state)
    box.clients.core.api.read_namespaced_pod_log.side_effect = lambda **kw: response()
    box.clients.core.api.list_namespaced_event.return_value = SimpleNamespace(items=[
        SimpleNamespace(reason="Unhealthy", message="Readiness probe failed: HTTP 503", type="Warning")])

    def choose(prompt):
        pods = [r["resource_ref"] for r in prompt["resources"] if r["kind"] == "pod"]
        count = len(model.prompts)
        if count == 5:
            assert prompt["terminal_only"] and not prompt["resources"] and not prompt["tool_guide"]
            assert len([e for e in prompt["evidence"] if e["resource_type"] == "PodLogs"]) == 4
            assert all(e.get("parse_status") != "unparsed" for e in prompt["evidence"])
            return stop(prompt)
        tool = "pod_events" if count == 3 else "pod_logs"
        requests = [{"tool": tool, "resource_ref": ref} for ref in pods[:2]]
        if tool == "pod_logs":
            for request in requests:
                request.update(previous=count == 4, tail_lines=100)
        return {"action": "collect", "reason": "Inspect distinct evidence sources",
                "missing_fact": "Current or previous instance errors and probe events",
                "evidence_ids": prompt["available_evidence_ids"][:1], "requests": requests}

    model = Model(choose)
    with session(case, model) as (_, _, advance):
        result = advance()
    assert result["output"]["status"] == "stop"
    assert len(model.prompts) == 5 and len(result["observations"]) == 6
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 4
    assert box.clients.core.api.list_namespaced_event.call_count == 2
    for call in box.clients.core.api.read_namespaced_pod_log.call_args_list:
        assert call.kwargs["_preload_content"] is False
    cards = [evidence_card(row) for row in result["observations"]]
    assert all(card["parse_status"] == "parsed" and card["coverage"] == "partial" for card in cards)
    assert all(estimate(p) <= INPUT_LIMIT for p in model.prompts)
    with session(case, Model(lambda _: pytest.fail("paid replay"))) as (_, _, advance):
        assert advance() == result
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 4
    audit = os.environ.get("INCIDENT_AGENT_TEST_AUDIT_DIR")
    if audit:
        (Path(audit) / "c2-robustness.json").write_text(json.dumps({
            "model_calls": 5, "tool_calls": 6, "parsed_cards": 6,
            "final_reached": True, "replay": "passed",
            "input_estimates": [estimate(p) for p in model.prompts],
        }), encoding="utf-8")


def test_compact_retry_preserves_required_evidence(case, monkeypatch):
    from backend.app.investigation import context
    original = context.card_block

    def large_view(item, **kwargs):
        block = original(item, **kwargs)
        if not kwargs.get("compact"):
            block["excerpt"] = json.dumps({"large_optional_detail": "x" * 60000})
        return block

    monkeypatch.setattr(context, "card_block", large_view)
    prompt = context.build_context(case[1].state, case[1].manifest(), [])
    assert prompt["context_view"] == "compact"
    facts = prompt["policy_facts"]
    required = facts["resource_evidence_ids"] + facts["business_evidence_ids"] + facts["configuration_evidence_ids"]
    assert set(required) <= set(prompt["available_evidence_ids"])
    assert estimate(prompt) <= INPUT_LIMIT
