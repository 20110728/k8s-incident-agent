"""Regression for incident 7b337c26: top-level citations did not bind the claim.

Synthetic model responses only. No incident logs, credentials, or live writes.
"""
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.app.agent.diagnosis_policy import InvalidDiagnosisAssessment, validate_diagnosis_assessment
from backend.app.agent.nodes import make_diagnose_incident_node
from backend.app.agent.remediation_policy import get_allowed_remediation_actions
from backend.app.agent.schemas import CurrentDiagnosis
from backend.app.llm.context_builder import build_diagnosis_context
from backend.tests.diagnosis_policy.test_stage4 import state, drift


def incomplete_claim(state):
    drift(state, 'patch_readiness_probe')
    output = CurrentDiagnosis.model_validate(state['diagnosis'])
    output.assessment.root_cause_hypotheses[0].evidence_ids = ['ev-test-002']
    assert 'ev-test-001' in output.evidence_ids
    return output


def response(diagnosis):
    return SimpleNamespace(diagnosis=diagnosis, model_name='synthetic', usage={'total_tokens': 10})


def test_top_level_service_reference_does_not_complete_supported_claim(state):
    output = incomplete_claim(state)
    original = copy.deepcopy(output.model_dump())
    with pytest.raises(InvalidDiagnosisAssessment, match='SUPPORTED_CONFIGURATION_REFERENCES_INCOMPLETE') as exc:
        validate_diagnosis_assessment(output, state)
    assert 'ev-test-001' in str(exc.value)
    assert output.model_dump() == original  # No automatic evidence attachment.


def test_model_can_correct_claim_in_existing_one_retry(state):
    invalid = incomplete_claim(state)
    corrected = invalid.model_copy(deep=True)
    corrected.assessment.root_cause_hypotheses[0].evidence_ids.append('ev-test-001')
    model = Mock()
    model.diagnose.side_effect = [response(invalid), response(corrected)]
    result = make_diagnose_incident_node(model)(state)
    assert result['phase'] == 'diagnosis_completed' and result['diagnosis_retry_count'] == 1
    assert model.diagnose.call_count == 2 and result['llm_usage']['total_tokens'] == 20
    assert 'ev-test-001' in model.diagnose.call_args.args[0]['diagnosis_validation_feedback']
    assert 'patch_readiness_probe' in get_allowed_remediation_actions({**state, **result})


def test_model_can_remain_uncertain_without_authorizing_write(state):
    invalid = incomplete_claim(state)
    uncertain = invalid.model_copy(deep=True)
    uncertain.assessment.root_cause_hypotheses[0].status = 'suspected'
    model = Mock()
    model.diagnose.side_effect = [response(invalid), response(uncertain)]
    result = make_diagnose_incident_node(model)(state)
    assert result['phase'] == 'diagnosis_completed'
    assert get_allowed_remediation_actions({**state, **result}) == {'manual_investigation'}


def test_two_incomplete_claims_fail_instead_of_silent_manual_fallback(state):
    invalid = incomplete_claim(state)
    model = Mock()
    model.diagnose.return_value = response(invalid)
    result = make_diagnose_incident_node(model)(state)
    assert result['phase'] == 'diagnosis_failed' and result['diagnosis'] is None
    assert model.diagnose.call_count == 2
    assert result['errors'][0]['code'] == 'INVALID_DIAGNOSIS_ASSESSMENT'
    assert 'SUPPORTED_CONFIGURATION_REFERENCES_INCOMPLETE' in result['errors'][0]['message']


def test_complete_first_response_does_not_add_a_retry(state):
    drift(state, 'patch_readiness_probe')
    model = Mock()
    model.diagnose.return_value = response(CurrentDiagnosis.model_validate(state['diagnosis']))
    result = make_diagnose_incident_node(model)(state)
    assert result['phase'] == 'diagnosis_completed' and result['diagnosis_retry_count'] == 0
    model.diagnose.assert_called_once()


def test_output_contract_explains_claim_level_references(state):
    assert '该假设自身的 evidence_ids' in build_diagnosis_context(state)
