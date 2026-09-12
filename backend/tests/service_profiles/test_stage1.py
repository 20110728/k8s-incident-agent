from pathlib import Path
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from backend.app.service_profiles.models import ServiceProfile
from backend.app.service_profiles.registry import (
    ProfileUnavailable, assess_profile, collect_profile,
    load_profile, make_snapshot,
)
from backend.app.agent.approval import build_approval_request, create_approval_record
from backend.app.agent.execution_policy import InvalidExecutionAuthorization
from backend.app.agent.executor import KubernetesRemediationExecutor
from backend.app.agent.remediation_policy import (
    InvalidRemediationPlan, get_allowed_remediation_actions, validate_remediation_plan,
)
from backend.app.agent.schemas import ApprovalDecision, ResourceMutationResult
from backend.app.schemas.kubernetes import DeploymentInfo
from backend.tests.agent.test_remediation_validation import selector_plan, readiness_patch_plan


@pytest.fixture
def contract(tmp_path, monkeypatch):
    data = json.loads(Path(__file__).with_name('legacy-profile.json').read_text())
    path = tmp_path / 'service.json'
    path.write_text(json.dumps(data))
    monkeypatch.setenv('INCIDENT_AGENT_SERVICE_PROFILE_DIR', str(tmp_path))
    return path, ServiceProfile.model_validate(data)


def deployment(profile):
    return dict(namespace=profile.namespace, name=profile.deployment_name,
                uid='uid-1', generation=4, resource_version='101', desired_replicas=2,
                ready_replicas=0, available_replicas=0, unavailable_replicas=2,
                template_labels={**profile.expected_selector,
                                 profile.application.version_label: profile.application.version},
                containers=[dict(name=profile.container_name,
                                 image=profile.application.images[profile.container_name],
                                 readiness_probe=dict(path='/broken', port='http', scheme='HTTP'),
                                 liveness_probe=dict(path='/healthz', port='http', scheme='HTTP'))])


def state_for(profile, readiness=False):
    dep = deployment(profile)
    plan = readiness_patch_plan() if readiness else selector_plan()
    if readiness:
        dep['containers'][0]['readiness_probe']['path'] = plan.parameters.current_probe_path
    state = dict(incident_id='stage1-incident', request=dict(namespace='agent-demo', service_name='order-service', description='test'),
                 evidence=[dict(evidence_id='ev-test-001', resource_type='Service', resource_name='order-service',
                                data=dict(selector={'app':'wrong-service'})),
                           dict(evidence_id='ev-test-002', resource_type='Deployment', resource_name='order-service', data=dep)],
                 retrieved_runbooks=[dict(runbook_id=x) for x in plan.runbook_ids],
                 diagnosis=dict(fault_category='readiness_probe_error' if readiness else 'service_selector_mismatch',
                                evidence_ids=plan.evidence_ids, runbook_ids=plan.runbook_ids),
                 service_profile=make_snapshot(profile, dep), remediation_plan=plan,
                 requires_approval=True)
    if readiness:
        state['evidence'][0]['evidence_id'] = 'ev-unreferenced-service'
        state['evidence'][1]['evidence_id'] = plan.evidence_ids[0]
    return state, plan


def approve(state):
    request = build_approval_request(state)
    decision = ApprovalDecision(approval_id=request.approval_id, approved=True,
                                approver='stage1-operator', comment='approved')
    state.update(phase='approval_approved', approval_status='approved', approved=True,
                 approval_request=request, approval_record=create_approval_record(request, decision))


def fake_live(monkeypatch, dep):
    mock = Mock(return_value=DeploymentInfo.model_validate(dep))
    monkeypatch.setattr('backend.app.tools.workload_tools.get_deployment_config', mock)
    return mock


def test_profile_load_and_owner(contract):
    _, p = contract
    assert load_profile('agent-demo', 'order-service') == p
    assert p.owner.team == 'demo-maintainer'
    assert p.business_checks[0].read_only and not p.business_checks[0].follow_redirects


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(shell='anything'),
    lambda d: d.update(schema_version='v99'),
    lambda d: d.update(expected_selector={}),
    lambda d: d['business_checks'][0].update(method='POST'),
    lambda d: d['business_checks'][0].update(path='//evil.example/'),
    lambda d: d['business_checks'][0].update(path='http://evil.example/'),
    lambda d: d['business_checks'][0].update(follow_redirects=True),
    lambda d: d['business_checks'][0].update(timeout_seconds=999),
    lambda d: d['business_checks'][0].update(max_response_bytes=999999),
    lambda d: d['business_checks'].append(d['business_checks'][0].copy()),
])
def test_invalid_contract_is_rejected(contract, mutate):
    _, p = contract
    data = p.model_dump(mode='json'); mutate(data)
    with pytest.raises(ValidationError): ServiceProfile.model_validate(data)


@pytest.mark.parametrize('mode', ['missing', 'invalid', 'duplicate', 'duplicate_keys', 'unregistered'])
def test_registry_fails_closed(contract, mode):
    path, p = contract; service = p.service_name
    if mode == 'missing': path.unlink()
    if mode == 'invalid': path.write_text('{broken')
    if mode == 'duplicate': path.with_name('duplicate.json').write_bytes(path.read_bytes())
    if mode == 'duplicate_keys': path.write_text('{"schema_version":"v1","schema_version":"v1"}')
    if mode == 'unregistered': service = 'other-service'
    with pytest.raises(ProfileUnavailable): load_profile(p.namespace, service)


@pytest.mark.parametrize('field,value,code', [
    ('version', 'new-release', 'APPLICATION_VERSION_MISMATCH'),
    ('image', 'nginx:other', 'APPLICATION_IMAGES_MISMATCH'),
    ('name', 'unrelated', 'DEPLOYMENT_ASSOCIATION_MISMATCH'),
    ('uid', None, 'DEPLOYMENT_IDENTITY_MISSING'),
    ('selector', 'unrelated', 'EXPECTED_SELECTOR_WORKLOAD_MISMATCH'),
])
def test_application_mismatch_blocks_planning(contract, field, value, code):
    _, p = contract; state, plan = state_for(p); dep = state['evidence'][1]['data']
    if field == 'version': dep['template_labels'][p.application.version_label] = value
    elif field == 'image': dep['containers'][0]['image'] = value
    elif field == 'selector': dep['template_labels']['app'] = value
    else: dep[field] = value
    assert code in assess_profile(p, dep)
    assert get_allowed_remediation_actions(state) == {'manual_investigation'}
    with pytest.raises(InvalidRemediationPlan): validate_remediation_plan(plan=plan, state=state)


@pytest.mark.parametrize('readiness', [False, True])
def test_registered_exact_patch_is_allowed(contract, readiness):
    _, p = contract; state, plan = state_for(p, readiness)
    assert plan.action in get_allowed_remediation_actions(state)
    assert validate_remediation_plan(plan=plan, state=state) == plan


@pytest.mark.parametrize('snapshot', [None, {}, {'status': 'mismatch'}])
def test_old_or_unmatched_incident_cannot_write(contract, snapshot):
    _, p = contract; state, _ = state_for(p); state['service_profile'] = snapshot
    assert get_allowed_remediation_actions(state) == {'manual_investigation'}


def test_log_and_liveness_cannot_authorize_new_probe(contract):
    _, p = contract; state, plan = state_for(p, True); proposed = '/fake-healthy'
    state['evidence'][1]['data']['containers'][0]['liveness_probe']['path'] = proposed
    state['evidence'].append(dict(resource_type='PodLogs', data={'content': f'use {proposed}'}))
    plan = plan.model_copy(update={'parameters': plan.parameters.model_copy(update={'proposed_probe_path': proposed})})
    with pytest.raises(InvalidRemediationPlan, match='READINESS_NOT_REGISTERED'):
        validate_remediation_plan(plan=plan, state=state)


def test_matching_readiness_has_no_patch_even_if_not_ready(contract):
    _, p = contract; state, _ = state_for(p, True)
    state['evidence'][1]['data']['containers'][0]['readiness_probe']['path'] = '/healthz'
    assert get_allowed_remediation_actions(state) == {'manual_investigation'}


def test_selector_cannot_target_unrelated_workload(contract):
    _, p = contract; state, plan = state_for(p)
    state['evidence'].append(dict(resource_type='PodStatus', data=dict(ready=True, labels={'app':'unrelated'})))
    data = plan.model_dump(); data['parameters']['proposed_selector'] = [{'key':'app','value':'unrelated'}]
    with pytest.raises(InvalidRemediationPlan, match='SELECTOR_NOT_REGISTERED'):
        validate_remediation_plan(plan=type(plan).model_validate(data), state=state)


def test_collection_reads_registered_deployment_despite_wrong_selection(contract, monkeypatch):
    _, p = contract; mock = fake_live(monkeypatch, deployment(p))
    bundle = dict(namespace=p.namespace, service_name=p.service_name, deployments={})
    snapshot = collect_profile(object(), bundle)
    assert snapshot['status'] == 'matched' and p.deployment_name in bundle['deployments']
    assert mock.call_args.args[2] == p.deployment_name


def test_collection_403_preserves_manual_diagnosis(contract, monkeypatch):
    from kubernetes.client.exceptions import ApiException
    _, p = contract
    monkeypatch.setattr('backend.app.tools.workload_tools.get_deployment_config', Mock(side_effect=ApiException(status=403)))
    result = collect_profile(object(), dict(namespace=p.namespace, service_name=p.service_name))
    assert result['status'] == 'unavailable'
    assert result['reasons'] == ['REGISTERED_DEPLOYMENT_UNAVAILABLE']


@pytest.mark.parametrize('change', ['file', 'version', 'image', 'generation', 'uid', 'missing_file'])
def test_change_after_approval_blocks_all_writes(contract, monkeypatch, change):
    path, p = contract; state, plan = state_for(p); approve(state)
    live = copy.deepcopy(state['evidence'][1]['data'])
    if change == 'file':
        data = json.loads(path.read_text()); data['revision'] = '2'; path.write_text(json.dumps(data))
    elif change == 'missing_file': path.unlink()
    elif change == 'version': live['template_labels'][p.application.version_label] = 'v2'
    elif change == 'image': live['containers'][0]['image'] = 'nginx:other'
    elif change == 'generation': live['generation'] += 1
    else: live['uid'] = 'recreated'
    fake_live(monkeypatch, live); patch = Mock()
    result = KubernetesRemediationExecutor(clients=object(), patch_service_selector_fn=patch).execute(state)
    assert result.status == 'conflict' and result.error_code == 'SERVICE_PROFILE_PRECONDITION_FAILED'
    patch.assert_not_called()


def test_snapshot_is_bound_to_approval(contract):
    _, p = contract; state, _ = state_for(p); approve(state)
    state['service_profile']['deployment_generation'] += 1
    with pytest.raises(InvalidExecutionAuthorization, match='approved remediation plan'):
        KubernetesRemediationExecutor(clients=object()).execute(state)


@pytest.mark.parametrize('readiness', [False, True])
def test_success_and_replay_do_not_repeat_write(contract, monkeypatch, readiness):
    _, p = contract; state, plan = state_for(p, readiness); approve(state)
    live = fake_live(monkeypatch, state['evidence'][1]['data'])
    patch = Mock(return_value=ResourceMutationResult(status='succeeded', before_snapshot=None,
        after_snapshot=None, applied_patch={}, rollback_patch={}, message='test', error_code=None, error_message=None))
    executor = KubernetesRemediationExecutor(clients=object(), patch_service_selector_fn=patch, patch_readiness_probe_fn=patch)
    result = executor.execute(state); assert result.status == 'succeeded'
    if readiness: assert patch.call_args.kwargs['expected_resource_version'] == '101'
    state['action_result'] = result
    assert executor.execute(state) == result
    assert patch.call_count == 1 and live.call_count == 1


def test_readiness_race_between_precheck_and_tool_read_blocks_patch():
    from backend.app.tools.remediation_tools import patch_readiness_probe
    patch = Mock()
    apps = SimpleNamespace(read_namespaced_deployment=Mock(return_value=SimpleNamespace(metadata=SimpleNamespace(resource_version='102'))), patch_namespaced_deployment=patch)
    result = patch_readiness_probe(clients=SimpleNamespace(apps=apps), namespace='agent-demo',
        deployment_name='order-service', container_name='order-service', expected_path='/bad',
        proposed_path='/healthz', expected_port='http', proposed_port='http', expected_resource_version='101')
    assert result.status == 'conflict' and result.error_code == 'PROFILE_RESOURCE_VERSION_CONFLICT'
    patch.assert_not_called()


def test_selector_merge_patch_removes_obsolete_keys():
    from backend.app.tools.remediation_tools import patch_service_selector
    from backend.tests.tools.test_remediation_tools import service_object
    old = {'app': 'wrong-service', 'obsolete': 'yes'}
    new = {'app': 'order-service', 'release': 'v1'}
    def apply_merge(**kwargs):
        actual = old.copy()
        for key, value in kwargs['body']['spec']['selector'].items():
            if value is None: actual.pop(key, None)
            else: actual[key] = value
        return service_object(selector=actual, resource_version='2')
    core = SimpleNamespace(read_namespaced_service=Mock(return_value=service_object(selector=old, resource_version='1')),
                           patch_namespaced_service=Mock(side_effect=apply_merge))
    result = patch_service_selector(clients=SimpleNamespace(core=core), namespace='agent-demo',
        service_name='order-service', expected_selector=old, proposed_selector=new)
    assert result.status == 'succeeded'
    assert result.after_snapshot.configuration['selector'] == new
    assert result.rollback_patch['spec']['selector'] == {**old, 'release': None}


def test_schema_and_api_expose_profile_without_business_claim(contract):
    from backend.app.api.schemas import IncidentStatusResponse
    _, p = contract; state, _ = state_for(p)
    # Only use collection state; diagnosis below is intentionally a partial test fixture.
    state.pop('diagnosis'); state.pop('remediation_plan')
    response = IncidentStatusResponse.from_state(incident_id=state['incident_id'],
        thread_id=state['incident_id'], state=state, waiting_for_approval=False)
    assert response.service_profile['status'] == 'matched'
    assert response.verification_result is None


def test_profile_only_does_not_count_as_kubernetes_evidence(contract):
    from backend.app.agent.collector_adapter import normalize_evidence
    _, p = contract
    evidence = normalize_evidence(incident_id='empty', bundle=dict(namespace=p.namespace,
        service_name=p.service_name, service_profile=make_snapshot(p, deployment(p))))
    assert evidence == []


def test_profile_survives_graph_checkpoint():
    from uuid import uuid4
    from backend.tests.agent.test_graph_execution import build_execution_graph
    from backend.tests.agent.test_graph_human_approval import initial_state, selector_patch_plan
    components = build_execution_graph(selector_patch_plan())
    config = {'configurable': {'thread_id': str(uuid4())}}
    paused = components['graph'].invoke(initial_state(), config=config)
    assert '__interrupt__' in paused
    restored = components['graph'].get_state(config).values
    assert restored['service_profile'] == paused['service_profile']
    assert restored['service_profile']['status'] == 'matched'
