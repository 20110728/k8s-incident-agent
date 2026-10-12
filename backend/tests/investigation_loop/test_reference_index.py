"""Exact visible citations can repair an index, never semantic support."""
from copy import deepcopy

import pytest

from backend.app.investigation.graph import validate_decision, DiagnosisAssessmentRejected
from backend.app.investigation.diagnostics import debug_report
from backend.app.investigation.brief_debug import brief_debug_report
from backend.app.investigation.exchange import last_exchange_report
from backend.tests.diagnosis_policy.test_stage4 import state, drift


def proposal(state):
    drift(state, "patch_service_selector")
    raw = deepcopy(state["diagnosis"])
    raw["evidence_ids"].remove("ev-test-004")
    raw["reasoning_summary"] += " Pod observed: ev-test-004."
    raw["assessment"].update(missing_evidence=[], next_investigation=[])
    prompt = {"available_evidence_ids": [e["evidence_id"] for e in state["evidence"]],
              "available_runbook_ids": ["config-reference"]}
    return {"decision": {"action": "propose_plan", "candidate": "patch_service_selector", "diagnosis": raw}}, prompt


def test_index_repair_preserves_raw_and_exports_audit(state):
    value, prompt = proposal(state)
    original = deepcopy(value)
    note = {}
    result = validate_decision(value, prompt, state, None, False, normalization=note)
    assert value == original
    assert result["diagnosis"]["evidence_ids"] == [*original["decision"]["diagnosis"]["evidence_ids"], "ev-test-004"]
    assert result["diagnosis"]["assessment"] == original["decision"]["diagnosis"]["assessment"]
    assert note == {"kind": "declared_evidence_index", "added_evidence_ids": ["ev-test-004"]}
    again = {}
    assert validate_decision({"decision": result}, prompt, state, None, False, normalization=again) == result
    assert again == {}
    row = {"incident_id": "test", "run_id": "test", "output_snapshot": {}}
    data = {"calls": {"ticket": {"kind": "investigation_model", "status": "succeeded",
            "request_id": "test:decision:1", "reference_normalization": note, "result": {"parsed": original}}}}
    assert debug_report(row, data)["model_attempts"][0]["reference_normalization"] == note
    assert brief_debug_report(row, data)["model_attempts"][0]["reference_normalization"] == note
    exported = last_exchange_report(row, data)
    assert exported["reference_normalization"] == note
    assert exported["parsed_output"] == original


@pytest.mark.parametrize("mode", ["unknown", "hidden", "failed", "unsupported"])
def test_index_repair_does_not_bypass_reference_or_support_checks(state, mode):
    value, prompt = proposal(state)
    if mode == "unknown":
        value["decision"]["diagnosis"]["reasoning_summary"] += " ev-invented-999"
    elif mode == "hidden":
        prompt["available_evidence_ids"].remove("ev-test-004")
    elif mode == "failed":
        state["evidence"][3]["error"] = "READ_FAILED"
    else:
        value["decision"]["diagnosis"]["assessment"]["root_cause_hypotheses"][0]["evidence_ids"] = ["ev-test-001"]
    with pytest.raises(ValueError) as caught:
        validate_decision(value, prompt, state, None, False)
    if mode != "unsupported":
        assert not isinstance(caught.value, DiagnosisAssessmentRejected)


@pytest.mark.parametrize("action", ["patch_service_selector", "patch_readiness_probe"])
@pytest.mark.parametrize("follow_up", [False, True])
def test_configuration_claim_error_precedes_follow_up_requirement(state, action, follow_up):
    drift(state, action)
    raw = deepcopy(state["diagnosis"])
    if not follow_up:
        raw["assessment"].update(missing_evidence=[], next_investigation=[])
    claim = raw["assessment"]["root_cause_hypotheses"][0]
    claim["evidence_ids"].remove("ev-test-002")
    prompt = {"available_evidence_ids": [e["evidence_id"] for e in state["evidence"]],
              "available_runbook_ids": ["config-reference"]}
    value = {"decision": {"action": "propose_plan", "candidate": action, "diagnosis": raw}}
    original = deepcopy(value)
    with pytest.raises(DiagnosisAssessmentRejected, match="SUPPORTED_CONFIGURATION_REFERENCES_INCOMPLETE") as caught:
        validate_decision(value, prompt, state, None, False)
    assert "assessment.root_cause_hypotheses[0].evidence_ids" in str(caught.value)
    assert "ev-test-002" in str(caught.value)
    assert value == original  # The validator never supplies claim support.
    claim["evidence_ids"].append("ev-test-002")
    assert validate_decision(value, prompt, state, None, False)["action"] == "propose_plan"


@pytest.mark.parametrize("mode", ["suspected", "no_drift", "invented", "business_failed"])
def test_configuration_correction_does_not_grant_unsupported_write(state, mode):
    value, prompt = proposal(state)
    raw = value["decision"]["diagnosis"]
    raw["assessment"].update(missing_evidence=["Unverified cause"], next_investigation=["Verify cause"])
    if mode == "suspected":
        raw["assessment"]["root_cause_hypotheses"][0]["status"] = "suspected"
    elif mode == "no_drift":
        state["evidence"][0]["data"]["selector"] = {"app": "order-service"}
    elif mode == "invented":
        raw["assessment"]["root_cause_hypotheses"][0]["evidence_ids"].append("ev-invented-999")
    else:
        state["evidence"][5]["data"].update(status="failed", http_status=500, error_code="HTTP_STATUS_MISMATCH")
        raw["assessment"]["business_status"] = "failed"
    with pytest.raises(ValueError):
        validate_decision(value, prompt, state, None, False)
