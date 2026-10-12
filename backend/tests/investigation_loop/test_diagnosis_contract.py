"""Complete collection then correct a category conflict without inventing a cause."""
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.agent.diagnosis_policy import (
    DIAGNOSIS_DOMAINS, diagnosis_contract, diagnostic_facts,
    validate_diagnosis_assessment, InvalidDiagnosisAssessment,
)
from backend.app.agent.schemas import CurrentDiagnosis
from backend.tests.diagnosis_policy.test_stage4 import diagnosis, drift
from backend.tests.investigation_loop.test_loop import (
    case, storage, state, identified, toolbox, Model, conclusion,
)
from backend.tests.investigation_loop.test_c2_fix import rich_baseline
from backend.tests.investigation_loop.test_c2_robustness import response
from backend.tests.investigation_dialogue.test_dialogue import session


def test_probe_failure_without_drift_is_not_configuration_fault(state):
    state["evidence"][1]["data"]["ready_replicas"] = 0
    state["evidence"][3]["data"]["ready"] = False
    facts = diagnostic_facts(state)
    assert not facts["readiness_drift"]
    contract = diagnosis_contract(facts)
    assert contract["category_domains"] == DIAGNOSIS_DOMAINS
    assert "readiness_probe_error" in contract["blocked_configuration_categories"]
    value = diagnosis(state, "readiness_probe_error")
    with pytest.raises(InvalidDiagnosisAssessment, match="actual=False"):
        validate_diagnosis_assessment(value, state)
    value.assessment.problem_domain = "application_runtime"
    with pytest.raises(InvalidDiagnosisAssessment, match="actual_domain=application_runtime"):
        validate_diagnosis_assessment(value, state)


def test_actual_probe_configuration_drift_still_validates(state):
    drift(state, "patch_readiness_probe")
    assert "readiness_probe_error" not in diagnosis_contract(diagnostic_facts(state))["blocked_configuration_categories"]
    validate_diagnosis_assessment(CurrentDiagnosis.model_validate(state["diagnosis"]), state)


def test_three_rounds_then_invalid_category_corrects_to_grounded_unknown(case):
    budget, box, _ = case
    rich_baseline(box.state)
    box.clients.core.api.read_namespaced_pod_log.side_effect = lambda **_: response()
    box.clients.core.api.list_namespaced_event.return_value = SimpleNamespace(items=[
        SimpleNamespace(reason="Unhealthy", message="Readiness probe failed: HTTP 503", type="Warning")])

    def choose(prompt):
        contract = prompt["diagnosis_contract"]
        assert "readiness_probe_error" in contract["blocked_configuration_categories"]
        n = len(model.prompts)
        if n <= 3:
            ref = next(r["resource_ref"] for r in prompt["resources"] if r["kind"] == "pod")
            request = {"tool": "pod_events" if n == 2 else "pod_logs", "resource_ref": ref}
            if n != 2:
                request.update(previous=n == 3, tail_lines=100)
            return {"action": "collect", "reason": "Check current logs, events and previous instance",
                    "missing_fact": "Cause of readiness failure", "evidence_ids": prompt["available_evidence_ids"][:1],
                    "requests": [request]}
        assert prompt["terminal_only"] == (n == 5)
        value = conclusion(prompt, "unknown")
        if n == 4:
            value["diagnosis"]["fault_category"] = "readiness_probe_error"
            # Isolate the semantic conflict: configuration diagnoses must also
            # cite an available runbook, otherwise this tests a reference error.
            assert prompt["available_runbook_ids"]
            value["diagnosis"]["runbook_ids"] = prompt["available_runbook_ids"][:1]
            value["diagnosis"]["assessment"]["problem_domain"] = "application_runtime"
        else:
            assert "actual_domain=application_runtime" in prompt["feedback"]
            assert "readiness_drift=False" in prompt["feedback"]
            assert "unknown/insufficient_evidence" in prompt["feedback"]
        return value

    model = Model(choose)
    with session(case, model) as (_, _, advance):
        result = advance()
    assert len(model.prompts) == 5 and len(result["observations"]) == 3
    assert result["output"]["status"] == "conclude"
    final = result["output"]["decision"]["diagnosis"]
    assert final["fault_category"] == "unknown" and final["assessment"]["missing_evidence"]
    assert final["assessment"]["resource_status"] == "not_ready"
    assert final["assessment"]["business_status"] == "unknown"
    assert not result["output"]["cluster_writes_executed"]
    with session(case, Model(lambda _: pytest.fail("paid replay"))) as (_, _, advance):
        assert advance() == result
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 2
    assert box.clients.core.api.list_namespaced_event.call_count == 1
    with budget.edit() as data:
        failed = [c for c in data["calls"].values() if c.get("validation")]
        assert len(failed) == 1
        assert failed[0]["validation"]["error_type"] == "DiagnosisAssessmentRejected"
        assert data["investigation"].get("diagnosis_correction_for")
        assert not data["investigation"].get("correction_for")
        assert failed[0]["validation_context"]["diagnosis_facts"]["readiness_drift"] is False
        saved = deepcopy(data)
    from backend.app.investigation.brief_debug import brief_debug_report
    brief = brief_debug_report({"output_snapshot": result}, saved)
    rejected = next(c for c in brief["model_attempts"] if c.get("validation"))
    assert rejected["problem_domain"] == "application_runtime"
    assert rejected["diagnosis_facts"]["readiness_drift"] is False
    audit = os.environ.get("INCIDENT_AGENT_TEST_AUDIT_DIR")
    if audit:
        (Path(audit) / "diagnosis-contract.json").write_text(json.dumps({
            "model_calls": 5, "tool_calls": 3, "outcome": "conclude",
            "category": "unknown", "correction": "passed", "replay": "passed",
        }), encoding="utf-8")
