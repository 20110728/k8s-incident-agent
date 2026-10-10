"""Full original bodies and logs through the real ledger."""
import json
import os
from pathlib import Path

import pytest

from backend.app.investigation.context import build_context, estimate, INPUT_LIMIT
from backend.app.investigation.resampling import authorize_sample
from backend.app.tools.investigation_requests import ToolRequest
from backend.tests.investigation_loop.test_loop import (
    case, storage, state, identified, toolbox, Model, conclusion,
)
from backend.tests.investigation_dialogue.test_dialogue import session


def test_raw_fields_reach_model_without_card_field_selection(case):
    box = case[1]
    box.state["evidence"][0]["data"]["diagnostic_detail"] = "original server detail"
    box.state["evidence"][0]["data"]["token"] = "private-value"
    prompt = build_context(box.state, box.manifest(), [])
    row = next(r for r in prompt["evidence"] if r["evidence_id"] == box.state["evidence"][0]["evidence_id"])
    assert row["projection"] == "saved-evidence-raw-v1"
    assert json.loads(row["excerpt"])["diagnostic_detail"] == "original server detail"
    assert "private-value" not in json.dumps(prompt) and estimate(prompt) > 0


def test_first_request_reads_all_available_lines_then_conclusion_and_replay(case):
    budget, box, _ = case
    box.clients.core.api.read_namespaced_pod_log.side_effect = lambda **kw: ("raw line\n" * 1500).encode()
    requests = []
    def choose(prompt):
        n = len(model.prompts)
        if n == 1:
            ref = next(r["resource_ref"] for r in prompt["resources"] if r["kind"] == "pod")
            request = {"tool": "pod_logs", "resource_ref": ref, "previous": False, "tail_lines": 50}
            requests.append(request)
            return {"action": "collect", "reason": "Inspect all available logs", "missing_fact": "Earlier errors",
                    "evidence_ids": prompt["available_evidence_ids"][:1], "requests": [request]}
        logs = [json.loads(e["excerpt"]) for e in prompt["evidence"] if e["resource_type"] == "PodLogs"]
        assert logs[0]["content"].count("\n") == 1500
        return conclusion(prompt, "unknown")
    model = Model(choose)
    with session(case, model) as (_, _, advance):
        result = advance()
    assert result["output"]["status"] == "conclude" and len(model.prompts) == 2
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 1
    assert requests[0]["tail_lines"] == 50  # Even an explicit small model request is normalized.
    assert "tail_lines" not in box.clients.core.api.read_namespaced_pod_log.call_args.kwargs
    assert result["history"][0]["requests"][0]["tail_lines"] is None
    with pytest.raises(ValueError, match="RESAMPLE_REASON_REQUIRED"):
        authorize_sample(budget, box, requests[-1], "third", None, [])
    assert ToolRequest.model_validate({**requests[-1], "tail_lines": 100000}).tail_lines == 100000
    with session(case, Model(lambda _: pytest.fail("paid replay"))) as (_, _, advance):
        assert advance() == result
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 1
    if os.environ.get("INCIDENT_AGENT_TEST_AUDIT_DIR"):
        (Path(os.environ["INCIDENT_AGENT_TEST_AUDIT_DIR"]) / "raw-evidence.json").write_text(
            json.dumps({"lines": 1500, "tool_reads": 1, "conclusion": "unknown", "replay": "passed"}), encoding="utf-8")


def test_large_model_result_and_more_than_two_requests_are_not_length_rejected(case):
    from backend.app.investigation.contracts import Decision
    from backend.app.investigation.model import call_model
    from backend.tests.investigation_loop.test_loop import tool_request
    budget, box, _ = case
    prompt = build_context(box.state, box.manifest(), [])
    requests = [tool_request(prompt, index=0), tool_request(prompt, index=1), tool_request(prompt, tool="pod_events")]
    value = {"action": "collect", "reason": "reason " * 10000, "missing_fact": "application fault",
             "evidence_ids": prompt["available_evidence_ids"], "requests": requests}
    Decision.model_validate({"decision": value})
    model = Model(lambda _: value)
    first = call_model(model, budget, prompt, "large-output")
    second = call_model(Model(lambda _: pytest.fail("paid replay")), budget, prompt, "large-output")
    assert first == second and first["parsed"]["decision"] == value
    assert len(model.prompts) == 1
