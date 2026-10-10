"""Fresh tool evidence must reach the next decision under a crowded baseline."""
from copy import deepcopy
import json

import pytest

from backend.app.investigation.context import build_context, estimate, INPUT_LIMIT
from backend.app.investigation.diagnostics import debug_report
from backend.tests.investigation_loop.test_loop import (
    case, storage, state, identified, toolbox, Model, collect, stop, tool_request,
)
from backend.tests.investigation_dialogue.test_dialogue import session


def two_logs(prompt):
    value = collect(prompt)
    value["requests"].append(tool_request(prompt, index=1))
    return value


def crowd_baseline(state):
    for row in state["evidence"]:
        row["data"]["extra_description"] = "旧的采样背景信息" * 1800


def test_both_new_log_excerpts_survive_large_baseline_and_only_visible_ids_are_citable(case):
    _, box, _ = case
    current = deepcopy(box.state)
    crowd_baseline(current)
    for n in range(2):
        current["evidence"].append({"evidence_id": f"ev-new-{n}", "resource_type": "PodLogs",
            "resource_name": f"pod-{n}", "request_id": f"tool:{n}", "coverage": "partial", "truncated": True,
            "data": {"container_name": "order-service", "previous": False,
                     "content": f"dependency-{n} connection refused\n" * 600}, "error": None})
    history = [{"step": 1, "action": "collect", "results": [{"tool": "pod_logs",
        "evidence_ids": [f"ev-new-{n}"], "coverage": "partial", "error_code": None} for n in range(2)]}]
    prompt = build_context(current, box.manifest(), history)
    evidence = {e["evidence_id"]: e for e in prompt["evidence"]}
    assert estimate(prompt) > 0
    assert set(prompt["available_evidence_ids"]) == set(evidence)
    for n in range(2):
        assert f"dependency-{n} connection refused" in evidence[f"ev-new-{n}"]["excerpt"]
        assert not evidence[f"ev-new-{n}"]["excerpt_truncated"]
        assert evidence[f"ev-new-{n}"]["source_truncated"]
    assert json.loads(json.dumps(prompt)) == prompt


def test_live_loop_exposes_both_results_and_records_context_without_full_logs(case):
    budget, box, _ = case
    crowd_baseline(box.state)
    box.clients.core.api.read_namespaced_pod_log.return_value = "dependency connection refused\n" * 600
    def choose(prompt):
        if not prompt["history"]:
            return two_logs(prompt)
        assert len(prompt["history"][0]["requests"]) == 2
        log_rows = [e for e in prompt["evidence"] if e["resource_type"] == "PodLogs"]
        assert len(log_rows) == 2
        assert all("dependency connection refused" in e["excerpt"] for e in log_rows)
        return stop(prompt)
    model = Model(choose)
    with session(case, model) as (_, _, advance):
        result = advance()
    assert len(model.prompts) == 2 and len(result["observations"]) == 2
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 2
    with budget.edit() as data:
        report = debug_report({"output_snapshot": result}, data)
    second = report["model_attempts"][1]["metadata"]
    assert sum(e["resource_type"] == "PodLogs" for e in second["context_evidence"]) == 2
    assert len(report["tool_attempts"]) == 2
    assert "dependency connection refused" not in json.dumps(report)
    # Reopen the real PostgreSQL checkpointer on the same thread. A completed
    # checkpoint must be returned without invoking a model or rereading tools.
    with session(case, Model(lambda _: pytest.fail("replay invoked provider"))) as (_, _, advance):
        assert advance() == result
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 2


@pytest.mark.parametrize("correct", [True, False])
def test_unjustified_repeat_uses_only_shared_correction_without_extra_reads(case, correct):
    def choose(prompt):
        if prompt["feedback"] and correct:
            assert "RESAMPLE_REASON_REQUIRED" in prompt["feedback"]
            return stop(prompt)
        return two_logs(prompt)
    model = Model(choose)
    with session(case, model) as (_, _, advance):
        result = advance()
    assert len(model.prompts) == 3  # initial, repeated decision, one correction
    assert len(result["observations"]) == 2
    assert case[1].clients.core.api.read_namespaced_pod_log.call_count == 2
    if correct:
        assert result["output"]["status"] == "stop"
    else:
        assert result["output"]["stop_reason"] == "DECISION_VALIDATION_FAILED"
        assert all("RESAMPLE_REASON_REQUIRED" in failure["detail"] for failure in result["output"]["validation_failures"])


def test_latest_failed_evidence_kept_despite_old_input_limit(case, monkeypatch):
    from backend.app.investigation import context
    box = case[1]
    current = deepcopy(box.state)
    current["evidence"].append({"evidence_id": "ev-fresh", "resource_type": "ToolObservation",
        "resource_name": "pod", "request_id": "fresh", "data": {}, "error": "ACCESS_DENIED"})
    history = [{"results": [{"evidence_ids": ["ev-fresh"]}]}]
    monkeypatch.setattr(context, "INPUT_LIMIT", 1)
    prompt = build_context(current, box.manifest(), history)
    assert "ev-fresh" in prompt["available_evidence_ids"]
    assert not prompt["omitted_evidence_ids"]
    assert next(e for e in prompt["evidence"] if e["evidence_id"] == "ev-fresh")["error"] == "ACCESS_DENIED"
