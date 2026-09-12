from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.tests.diagnosis_policy.test_stage4 import state, diagnosis
from backend.app.agent.nodes import make_diagnose_incident_node
from backend.app.agent.diagnosis_policy import (
    diagnostic_facts, insufficient_evidence_diagnosis, InvalidDiagnosisAssessment,
)
from backend.app.agent.remediation_policy import get_allowed_remediation_actions
from backend.app.agent.graph import route_after_diagnosis


def uncertain(state):
    state['evidence'][1]['data']['ready_replicas'] = 0
    state['evidence'][3]['data']['ready'] = False
    state['evidence'][5]['data'].update(status='unknown', error_code='CONNECTION_ERROR', http_status=None, content_matches=None)


def execute(state, responses):
    service = Mock()
    service.diagnose.side_effect = [SimpleNamespace(diagnosis=d, usage={'total_tokens':10}, model_name='mock-only') for d in responses]
    return make_diagnose_incident_node(service)(state), service


@pytest.mark.parametrize("bad_output", ["application_error", "nested_reference", "model_unavailable"])
def test_precheck_does_not_call_model_or_consume_bad_output(state, bad_output):
    uncertain(state)
    bad = diagnosis(state, 'application_error')
    bad.root_cause = '未经证实的数据库损坏'
    if bad_output == 'nested_reference':
        bad.assessment.symptoms[0].evidence_ids = [' ev-test-006 ', 'ev-fake-999']
    service = Mock()
    service.diagnose.side_effect = RuntimeError('must never call model') if bad_output == 'model_unavailable' else None
    service.diagnose.return_value = SimpleNamespace(diagnosis=bad, usage={'total_tokens':10}, model_name='mock-only')
    result = make_diagnose_incident_node(service)(state)
    assert result['phase'] == 'diagnosis_completed'
    assert result['diagnosis']['fault_category'] == 'unknown'
    assert result['diagnosis']['confidence'] == 0.0
    assert result['diagnosis']['assessment']['resource_status'] == 'not_ready'
    assert result['diagnosis']['assessment']['business_status'] == 'unknown'
    assert result['diagnosis']['assessment']['root_cause_hypotheses'] == []
    assert '数据库损坏' not in str(result['diagnosis'])
    assert '本地证据规则' in result['diagnosis']['root_cause']
    assert any(t['step'] == 'diagnosis_policy_precheck' for t in result['trace'])
    service.diagnose.assert_not_called()
    assert result['llm_usage'] == {} and result['llm_model'] is None
    assert result['diagnosis_retry_count'] == 0
    assert get_allowed_remediation_actions({**state, **result}) == set()
    assert route_after_diagnosis(result) == 'skip'


def test_other_scope_still_uses_model_and_retry(state):
    state['evidence'][5]['data']['status'] = 'unknown'
    result, service = execute(state, [diagnosis(state, 'application_error'), diagnosis(state, 'unknown')])
    assert result['phase'] == 'diagnosis_completed'
    assert service.diagnose.call_count == 2
    assert not any(t['step'] == 'diagnosis_policy_precheck' for t in result['trace'])


def test_fabricated_reference_is_rejected_when_model_is_used(state):
    state['evidence'][5]['data']['status'] = 'unknown'
    bad = diagnosis(state, 'application_error')
    bad.evidence_ids.append('ev-fake-999')
    result, _ = execute(state, [bad, bad])
    assert result['phase'] == 'diagnosis_failed'
    assert result['errors'][0]['code'] == 'INVALID_DIAGNOSIS_REFERENCE'


def test_false_healthy_diagnosis_is_still_rejected(state):
    state['evidence'][5]['data']['status'] = 'unknown'
    bad = diagnosis(state)
    result, _ = execute(state, [bad, bad])
    assert result['phase'] == 'diagnosis_failed'


@pytest.mark.parametrize('mode', ['business_failed', 'profile_mismatch', 'configuration_drift', 'no_business_evidence', 'runtime_fault', 'missing_probe'])
def test_precheck_scope_is_narrow(state, mode):
    uncertain(state)
    if mode == 'business_failed': state['evidence'][5]['data'].update(status='failed', http_status=500, error_code='HTTP_STATUS_MISMATCH')
    if mode == 'profile_mismatch': state['service_profile']['status'] = 'mismatch'
    if mode == 'configuration_drift': state['evidence'][0]['data']['selector'] = {'app':'wrong'}
    if mode == 'no_business_evidence': state['evidence'].pop()
    if mode == 'runtime_fault': state['evidence'][3]['data']['containers'][0].update(state='waiting', waiting_reason='CrashLoopBackOff')
    if mode == 'missing_probe': state['evidence'][1]['data']['containers'][0]['readiness_probe'] = None
    with pytest.raises(InvalidDiagnosisAssessment): insufficient_evidence_diagnosis(state)
