"""Saved audit projection must not infer recovery or leak diagnostic context."""
from copy import deepcopy
import json

import pytest

from backend.app.runtime.remediation_export import remediation_report


@pytest.mark.parametrize("operation_state", [None, "prepared", "outcome_unknown", "manual_required", "succeeded"])
def test_export_preserves_uncertainty_and_redacts_without_mutating(operation_state):
    row = {"incident_id": "incident", "run_id": "run", "status": "succeeded",
           "output_snapshot": {"diagnosis": {"root_cause": "DIAGNOSIS_ONLY"},
                               "evidence": [{"data": "RAW_INPUT_ONLY"}],
                               "remediation_plan": {"action": "patch_service_selector"},
                               "trace": [{"step": "execute_remediation", "message": "Bearer private-value"},
                                         {"step": "diagnose_incident", "message": "DIAGNOSIS_ONLY"}]}}
    operation = {"incident_id": "incident", "run_id": "run", "state": operation_state,
                 "response_snapshot": {"token": "private-value"}} if operation_state else None
    original = deepcopy((row, operation))
    report = remediation_report(row, operation)
    assert (row, operation) == original
    assert report["verification_result"] is None
    assert report["availability"]["published_verification"] is False
    assert report["operation"] is None if operation is None else report["operation"]["state"] == operation_state
    text = json.dumps(report)
    assert "private-value" not in text
    assert "DIAGNOSIS_ONLY" not in text and "RAW_INPUT_ONLY" not in text


def test_export_refuses_cross_run_operation():
    with pytest.raises(ValueError, match="OPERATION_RUN_MISMATCH"):
        remediation_report({"incident_id": "one", "run_id": "run"}, {"incident_id": "other", "run_id": "run"})
