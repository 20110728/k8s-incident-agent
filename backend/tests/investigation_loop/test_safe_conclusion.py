"""A spent collection correction cannot discard the final evidence report."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.tests.investigation_loop.test_loop import (
    case, storage, state, identified, toolbox, Model, conclusion,
)
from backend.tests.investigation_loop.test_c2_fix import rich_baseline
from backend.tests.investigation_loop.test_c2_robustness import response
from backend.tests.investigation_dialogue.test_dialogue import session


@pytest.mark.parametrize("corrected", [True, False])
def test_collection_and_diagnosis_have_separate_correction_then_finish(case, corrected):
    budget, box, _ = case
    rich_baseline(box.state)
    box.clients.core.api.read_namespaced_pod_log.side_effect = lambda **_: response()
    box.clients.core.api.list_namespaced_event.return_value = SimpleNamespace(items=[])

    def choose(prompt):
        n = len(model.prompts)
        if n <= 4:
            ref = next(r["resource_ref"] for r in prompt["resources"] if r["kind"] == "pod")
            request = {"tool": "pod_events" if n == 3 else "pod_logs", "resource_ref": ref}
            if n != 3:
                request.update(previous=n == 4, tail_lines=100)
            return {"action": "collect", "reason": "Inspect evidence", "missing_fact": "Readiness failure cause",
                    "evidence_ids": prompt["available_evidence_ids"][:1], "requests": [request]}
        value = conclusion(prompt, "unknown")
        assert prompt["terminal_only"] == (n == 6)
        if n == 5 or not corrected:
            value["diagnosis"].update(fault_category="application_error", root_cause="UNSUPPORTED_MODEL_CAUSE")
            value["diagnosis"]["assessment"]["problem_domain"] = "application_runtime"
        return value

    model = Model(choose)
    with session(case, model) as (_, _, advance):
        result = advance()
    output = result["output"]
    assert len(model.prompts) == 6 and len(result["observations"]) == 3
    assert output["status"] == "conclude" and not output["cluster_writes_executed"]
    value = output["decision"]["diagnosis"]
    assert value["fault_category"] == "unknown" and "UNSUPPORTED_MODEL_CAUSE" not in json.dumps(value)
    assert value["assessment"]["resource_status"] == "not_ready"
    if not corrected:
        assert output["diagnosis_source"] == "program_evidence_only"
        assert len(output["validation_failures"]) == 2
        assert value["confidence"] == 0 and not value["assessment"]["root_cause_hypotheses"]
    with session(case, Model(lambda _: pytest.fail("paid replay"))) as (_, _, advance):
        assert advance() == result
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 2
    with budget.edit() as data:
        assert data["investigation"]["correction_for"] != data["investigation"]["diagnosis_correction_for"]
        assert len([c for c in data["calls"].values() if c["kind"] == "investigation_model"]) == 6
    if not corrected and os.environ.get("INCIDENT_AGENT_TEST_AUDIT_DIR"):
        (Path(os.environ["INCIDENT_AGENT_TEST_AUDIT_DIR"]) / "safe-conclusion.json").write_text(
            json.dumps({"model_calls": 6, "outcome": "conclude", "category": "unknown", "replay": "passed"}), encoding="utf-8")
