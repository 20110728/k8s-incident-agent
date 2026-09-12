from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.tests.diagnosis_policy.test_stage4 import state, diagnosis
from backend.app.agent.nodes import make_diagnose_incident_node


def run(state, d):
    model = Mock()
    model.diagnose.return_value = SimpleNamespace(diagnosis=d, usage={'total_tokens':11}, model_name='mock-only')
    return make_diagnose_incident_node(model)(state), model


@pytest.mark.parametrize('location', ['reasoning_summary', 'root_cause', 'nested'])
def test_real_reference_omitted_from_index_is_reconciled(state, location):
    state['evidence'][5]['data'].update(status='failed',http_status=200,content_matches=False,error_code='JSON_CONTENT_MISMATCH')
    d = diagnosis(state, 'application_error')
    d.evidence_ids.remove('ev-test-003')
    if location == 'nested':
        d.assessment.symptoms[0].evidence_ids.append('ev-test-003')
    else:
        setattr(d, location, getattr(d, location) + ' 当前端点见 ev-test-003。')
    result, model = run(state, d)
    assert result['phase'] == 'diagnosis_completed'
    assert 'ev-test-003' in result['diagnosis']['evidence_ids']
    assert 'ev-test-003' not in d.evidence_ids  # preserve original model object
    assert result['diagnosis_retry_count'] == 0 and model.diagnose.call_count == 1
    assert 'indexed existing Evidence' in result['trace'][0]['message']
    assert result['diagnosis']['assessment']['business_status'] == 'failed'


@pytest.mark.parametrize('location', ['reasoning_summary', 'nested', 'top'])
def test_invented_reference_still_rejected(state, location):
    d = diagnosis(state, 'unknown')
    if location == 'reasoning_summary': d.reasoning_summary += ' 见 ev-fiction-999。'
    if location == 'nested': d.assessment.symptoms[0].evidence_ids.append('ev-fiction-999')
    if location == 'top': d.evidence_ids.append('ev-fiction-999')
    result, model = run(state, d)
    assert result['phase'] == 'diagnosis_failed'
    assert result['errors'][0]['code'] == 'INVALID_DIAGNOSIS_REFERENCE'
    assert model.diagnose.call_count == 2
    assert result['llm_model'] == 'mock-only'
    assert result['llm_usage'] == {'total_tokens':22}


def test_index_reconciliation_does_not_bypass_business_guard(state):
    state['evidence'][5]['data']['status'] = 'failed'
    d = diagnosis(state)
    d.evidence_ids.remove('ev-test-003')
    d.reasoning_summary += ' 见 ev-test-003。'
    result, _ = run(state, d)
    assert result['phase'] == 'diagnosis_failed'
    assert result['errors'][0]['code'] == 'INVALID_DIAGNOSIS_ASSESSMENT'
