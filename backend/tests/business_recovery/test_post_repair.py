"""确定性回归：模拟现场观察，不冒充真实集群或模型验收。"""
import copy
from unittest.mock import Mock

import pytest

from backend.tests.diagnosis_policy.test_stage4 import state
from backend.app.agent.business_recovery import BusinessRecoveryVerifier
from backend.app.agent.nodes import make_verify_recovery_node
from backend.app.agent.schemas import RecoveryVerificationResult
from backend.app.api.schemas import IncidentStatusResponse
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


def bundle_from_state(state):
    bundle = dict(namespace='agent-demo', service_name='order-service', errors=[],
                  service_profile=copy.deepcopy(state['service_profile']),
                  service_pod_names=['order-pod'], namespace_pod_names=['order-pod'],
                  endpoint_slices=[], deployments={}, owner_chains={}, pod_statuses={}, business_checks=[])
    for e in copy.deepcopy(state['evidence']):
        kind, name, data = e['resource_type'], e['resource_name'], e['data']
        if kind == 'Service': bundle['service'] = data
        elif kind == 'Deployment': bundle['deployments'][name] = data
        elif kind == 'OwnerChain': bundle['owner_chains'][name] = data['owner_chain']
        elif kind == 'PodStatus': bundle['pod_statuses'][name] = data
        elif kind == 'EndpointSlice': bundle['endpoint_slices'].append(data)
        elif kind == 'BusinessCheck': bundle['business_checks'].append(data)
    return bundle


@pytest.fixture
def setup(state):
    state['action_result'] = dict(action='patch_service_selector', applied_patch={'spec': {}}, status='succeeded')
    resource = RecoveryVerificationResult(execution_id='exec-'+'1'*16, action='patch_service_selector',
        status='succeeded', started_at='2026-09-13T00:00:00+00:00', finished_at='2026-09-13T00:00:01+00:00',
        attempts=1, message='resource checks passed')
    bundle = bundle_from_state(state)
    collector = Mock(); collector.collect.return_value = bundle
    verifier = BusinessRecoveryVerifier(Mock(verify=Mock(return_value=resource)), collector)
    return state, bundle, resource, collector, verifier


def test_pass_fresh_evidence_preserves_approval_input(setup):
    state, bundle, _, collector, verifier = setup
    original = copy.deepcopy(state)
    result = verifier.verify(state)
    assert (result.status, result.resource_status, result.business_status) == ('succeeded', 'ready', 'passed')
    assert state == original
    assert not ({e['evidence_id'] for e in state['evidence']} & {e['evidence_id'] for e in result.post_repair_evidence})
    assert result.post_repair_profile == bundle['service_profile']
    collector.collect.assert_called_once_with('agent-demo', 'order-service')


@pytest.mark.parametrize('code', ['HTTP_STATUS_MISMATCH', 'JSON_CONTENT_MISMATCH'])
def test_failed_interface_does_not_report_recovery(setup, code):
    state, bundle, _, _, verifier = setup
    bundle['business_checks'][0].update(status='failed', error_code=code, content_matches=False)
    result = verifier.verify(state)
    assert result.status == 'failed' and result.business_status == 'failed'
    assert result.resource_verification_status == 'succeeded'
    assert result.error_code == 'POST_REPAIR_BUSINESS_FAILED'


@pytest.mark.parametrize('mode', ['unknown', 'skipped', 'missing', 'duplicate', 'wrong_target'])
def test_unavailable_or_unbound_never_passes(setup, mode):
    state, bundle, _, _, verifier = setup
    if mode in {'unknown','skipped'}: bundle['business_checks'][0]['status'] = mode
    if mode == 'missing': bundle['business_checks'] = []
    if mode == 'duplicate': bundle['business_checks'] *= 2
    if mode == 'wrong_target': bundle['business_checks'][0]['target']['service_name'] = 'other'
    result = verifier.verify(state)
    assert result.status == 'failed' and result.business_status == 'unknown'


@pytest.mark.parametrize('status', ['failed','timeout','skipped'])
def test_resource_failure_skips_business(setup, status):
    state, _, resource, collector, verifier = setup
    resource.status = status
    result = verifier.verify(state)
    assert result.status == status and result.business_status == 'skipped'
    collector.collect.assert_not_called()


@pytest.mark.parametrize('field,value', [('digest','changed'),('deployment_uid','recreated'),('deployment_generation',99)])
def test_target_changes_invalidate_observation(setup,field,value):
    state,bundle,_,_,verifier=setup
    bundle['service_profile'][field]=value
    result=verifier.verify(state)
    assert result.status=='failed' and result.business_status=='unknown'
    assert result.error_code=='POST_REPAIR_OBSERVATION_INVALID'


def test_readiness_expected_generation_change(setup):
    state,bundle,resource,_,verifier=setup
    state['action_result']['action']='patch_readiness_probe';resource.action='patch_readiness_probe'
    bundle['service_profile']['deployment_generation'] += 1
    bundle['deployments']['order-service']['generation'] += 1
    assert verifier.verify(state).status=='succeeded'


def test_resource_regression_after_initial_success(setup):
    state,bundle,_,_,verifier=setup
    bundle['deployments']['order-service']['ready_replicas']=0
    result=verifier.verify(state)
    assert result.status=='failed' and result.resource_status=='not_ready'


def test_collection_exception_does_not_reuse_old_pass(setup):
    state,_,_,collector,verifier=setup
    collector.collect.side_effect=RuntimeError('unavailable')
    result=verifier.verify(state)
    assert result.status=='failed' and result.business_status=='unknown'
    assert not result.post_repair_evidence


def test_node_api_and_checkpoint_roundtrip(setup):
    state,_,_,_,verifier=setup
    update=make_verify_recovery_node(verifier)(state)
    assert update['phase']=='verification_succeeded'
    # 使用普通 JSON 结构检查新增内容的 checkpoint 序列化，不需要数据库。
    stored={**state,**update, 'action_result':None, 'diagnosis':None}
    stored['verification_result']=update['verification_result'].model_dump(mode='json')
    serializer=JsonPlusSerializer()
    restored=serializer.loads_typed(serializer.dumps_typed(stored))
    response=IncidentStatusResponse.from_state(incident_id=state['incident_id'], thread_id=state['incident_id'],
        state=restored,waiting_for_approval=False)
    assert response.verification_result.business_status=='passed'
    assert response.verification_result.post_repair_evidence


def test_legacy_success_does_not_claim_business(setup):
    _,_,resource,_,_=setup
    assert resource.verification_scope=='resource_only'
    assert resource.business_status=='skipped'


def test_production_factory_wraps_resource_verifier(monkeypatch):
    from backend.app.agent import dependencies
    monkeypatch.setattr(dependencies,'create_clients',lambda:Mock())
    assert isinstance(dependencies.build_recovery_verifier(),BusinessRecoveryVerifier)
