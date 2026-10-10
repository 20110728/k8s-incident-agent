"""Original bodies and one bounded log-window expansion through the real ledger."""
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
    assert "private-value" not in json.dumps(prompt) and estimate(prompt) <= INPUT_LIMIT


def test_first_request_is_thousand_lines_then_conclusion_and_replay(case):
    budget, box, _ = case
    box.clients.core.api.read_namespaced_pod_log.side_effect = lambda **kw: ("raw line\n" * kw["tail_lines"]).encode()
    requests = []
    def choose(prompt):
        n = len(model.prompts)
        if n == 1:
            ref = next(r["resource_ref"] for r in prompt["resources"] if r["kind"] == "pod")
            request = {"tool": "pod_logs", "resource_ref": ref, "previous": False, "tail_lines": 50 if n == 1 else 1000}
            requests.append(request)
            return {"action": "collect", "reason": "Expand log window once", "missing_fact": "Earlier errors",
                    "evidence_ids": prompt["available_evidence_ids"][:1], "requests": [request]}
        logs = [json.loads(e["excerpt"]) for e in prompt["evidence"] if e["resource_type"] == "PodLogs"]
        assert logs[0]["content"].count("\n") == 1000
        return conclusion(prompt, "unknown")
    model = Model(choose)
    with session(case, model) as (_, _, advance):
        result = advance()
    assert result["output"]["status"] == "conclude" and len(model.prompts) == 2
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 1
    assert requests[0]["tail_lines"] == 50  # Even an explicit small model request is normalized.
    assert box.clients.core.api.read_namespaced_pod_log.call_args.kwargs["tail_lines"] == 1000
    assert result["history"][0]["requests"][0]["tail_lines"] == 1000
    with pytest.raises(ValueError, match="RESAMPLE_REASON_REQUIRED"):
        authorize_sample(budget, box, requests[-1], "third", None, [])
    with pytest.raises(ValueError):
        ToolRequest.model_validate({**requests[-1], "tail_lines": 1001})
    with session(case, Model(lambda _: pytest.fail("paid replay"))) as (_, _, advance):
        assert advance() == result
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 1
    if os.environ.get("INCIDENT_AGENT_TEST_AUDIT_DIR"):
        (Path(os.environ["INCIDENT_AGENT_TEST_AUDIT_DIR"]) / "raw-evidence.json").write_text(
            json.dumps({"lines": 1000, "tool_reads": 1, "conclusion": "unknown", "replay": "passed"}), encoding="utf-8")
