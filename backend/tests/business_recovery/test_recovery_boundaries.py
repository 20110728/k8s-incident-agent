"""Stage 1 boundaries: synthetic observations, no model, network or cluster writes."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.agent.business_recovery import recovery_resource_view
from backend.app.agent.diagnosis_policy import diagnostic_facts, validate_diagnosis_assessment, InvalidDiagnosisAssessment
from backend.app.agent.remediation_policy import get_allowed_remediation_actions
from backend.app.service_profiles.models import ServiceProfile
from backend.app.service_profiles.registry import matched_profile, profile_digest
from backend.tests.business_recovery.test_post_repair import setup
from backend.tests.diagnosis_policy.test_stage4 import state, diagnosis, drift


def add_retiring_pod(bundle):
    pod = copy.deepcopy(bundle['pod_statuses']['order-pod'])
    pod.update(ready=False, pod_ip='10.244.0.19', containers=[
        dict(state='waiting', waiting_reason='CrashLoopBackOff')])
    bundle['pod_statuses']['old-order-pod'] = pod
    bundle['owner_chains']['old-order-pod'] = copy.deepcopy(bundle['owner_chains']['order-pod'])
    endpoint = dict(target_kind='Pod', target_name='old-order-pod', addresses=['10.244.0.19'],
                    terminating=True, ready=False, serving=False)
    bundle['endpoint_slices'][0]['endpoints'].append(endpoint)
    return pod, endpoint


def test_two_ready_replacements_allow_retiring_old_pod_without_erasing_evidence(setup):
    original, bundle, _, collector, verifier = setup
    add_retiring_pod(bundle)
    before = copy.deepcopy(original)
    result = verifier.verify(original)
    assert result.status == 'succeeded'
    assert result.resource_status == 'ready' and result.business_status == 'passed'
    assert any(e['resource_type'] == 'PodStatus' and e['resource_name'] == 'old-order-pod'
               for e in result.post_repair_evidence)
    assert any('old-order-pod' in scope for scope in result.unverified_scope)
    assert original == before
    collector.collect.assert_called_once()  # A single post-wait sample, no stability loop.


@pytest.mark.parametrize('mode', [
    'serving', 'serving_unknown', 'serving_absent', 'ready', 'ready_unknown',
    'terminating_false', 'terminating_unknown', 'ip_mismatch', 'ip_missing',
    'target_kind', 'no_endpoint', 'slice_namespace', 'slice_service', 'conflicting_records',
])
def test_ambiguous_or_still_serving_old_pod_is_not_excluded(setup, mode):
    original, bundle, _, _, verifier = setup
    pod, endpoint = add_retiring_pod(bundle)
    if mode == 'serving': endpoint['serving'] = True
    elif mode == 'serving_unknown': endpoint['serving'] = None
    elif mode == 'serving_absent': endpoint.pop('serving')
    elif mode == 'ready': endpoint['ready'] = True
    elif mode == 'ready_unknown': endpoint['ready'] = None
    elif mode == 'terminating_false': endpoint['terminating'] = False
    elif mode == 'terminating_unknown': endpoint['terminating'] = None
    elif mode == 'ip_mismatch': endpoint['addresses'] = ['10.244.0.99']
    elif mode == 'ip_missing': pod.pop('pod_ip')
    elif mode == 'target_kind': endpoint['target_kind'] = 'Node'
    elif mode == 'no_endpoint': bundle['endpoint_slices'][0]['endpoints'].remove(endpoint)
    elif mode == 'slice_namespace': bundle['endpoint_slices'][0]['namespace'] = 'other'
    elif mode == 'slice_service': bundle['endpoint_slices'][0]['service_name'] = 'other'
    elif mode == 'conflicting_records':
        # The conflicting record is in a different slice for the same Service.
        other_slice = copy.deepcopy(bundle['endpoint_slices'][0])
        other_slice['name'] = 'conflicting-slice'
        other_slice['endpoints'] = [{**endpoint, 'serving': True}]
        bundle['endpoint_slices'].append(other_slice)
    result = verifier.verify(original)
    assert result.status == 'failed' and result.resource_status == 'not_ready'
    assert not any(c.name == 'terminating_pods_classified' for c in result.checks)


@pytest.mark.parametrize('mode', ['unowned', 'wrong_pod_namespace', 'pod_ready', 'duplicate_status', 'profile_unbound'])
def test_exclusion_requires_one_owned_unready_pod_in_a_bound_snapshot(setup, mode):
    original, bundle, _, _, _ = setup
    pod, _ = add_retiring_pod(bundle)
    if mode == 'unowned': bundle['owner_chains']['old-order-pod']['deployment_name'] = 'other'
    elif mode == 'wrong_pod_namespace': pod['namespace'] = 'other'
    elif mode == 'pod_ready': pod['ready'] = True
    from backend.app.agent.collector_adapter import normalize_evidence
    observation = dict(request=original['request'], service_profile=bundle['service_profile'],
                       evidence=normalize_evidence(incident_id='boundary', bundle=bundle))
    if mode == 'duplicate_status':
        duplicate = copy.deepcopy(next(e for e in observation['evidence']
                                      if e['resource_type'] == 'PodStatus' and e['resource_name'] == 'old-order-pod'))
        duplicate['evidence_id'] = 'ev-boundary-999'
        observation['evidence'].append(duplicate)
    elif mode == 'profile_unbound': observation['service_profile'] = {'status': 'unavailable'}
    before = copy.deepcopy(observation)
    view, excluded = recovery_resource_view(observation)
    assert excluded == [] and view['evidence'] == before['evidence']
    assert observation == before


def test_retiring_pod_does_not_compensate_for_missing_replacement(setup):
    original, bundle, _, _, verifier = setup
    add_retiring_pod(bundle)
    bundle['pod_statuses'].pop('order-pod-2')
    bundle['deployments']['order-service'].update(ready_replicas=1, available_replicas=1)
    result = verifier.verify(original)
    assert result.status == 'failed' and result.resource_status == 'not_ready'
    assert result.business_status == 'passed'  # A successful request does not prove two ready replicas.


@pytest.mark.parametrize('replicas', [0, 1, 3])
def test_registered_two_replicas_cannot_be_redefined_by_live_scale(setup, replicas):
    original, bundle, _, _, verifier = setup
    bundle['deployments']['order-service'].update(
        desired_replicas=replicas, ready_replicas=replicas, available_replicas=replicas)
    result = verifier.verify(original)
    assert result.status == 'failed' and result.resource_status == 'not_ready'
    check = next(c for c in result.checks if c.name == 'registered_replica_count')
    assert not check.passed and check.expected == 2 and check.observed == replicas


@pytest.mark.parametrize('mode,status,drifted,patchable', [
    ('matching', 'matched', False, False), ('path', 'drift', True, True),
    ('port', 'drift', True, True), ('scheme', 'drift', True, False),
    ('no_http_probe', 'drift', True, False), ('missing_field', 'unknown', False, False),
    ('incomplete', 'unknown', False, False),
])
def test_probe_drift_is_distinct_from_patch_support(state, mode, status, drifted, patchable):
    container = state['evidence'][1]['data']['containers'][0]
    if mode in {'path', 'port', 'scheme'}:
        container['readiness_probe'][mode] = {'path': '/broken', 'port': 9999, 'scheme': 'HTTPS'}[mode]
    elif mode == 'no_http_probe': container['readiness_probe'] = None
    elif mode == 'missing_field': container.pop('readiness_probe')
    elif mode == 'incomplete': container['readiness_probe'].pop('port')
    facts = diagnostic_facts(state)
    assert facts['readiness_configuration_status'] == status
    assert facts['readiness_drift'] is drifted
    assert facts['readiness_patch_supported'] is patchable
    if mode != 'matching':
        with pytest.raises(InvalidDiagnosisAssessment):
            validate_diagnosis_assessment(diagnosis(state), state)


@pytest.mark.parametrize('mode', ['scheme', 'no_http_probe', 'missing_field', 'incomplete'])
def test_unsupported_or_unknown_probe_never_authorizes_readiness_write(state, mode):
    drift(state, 'patch_readiness_probe')
    container = state['evidence'][1]['data']['containers'][0]
    if mode == 'scheme': container['readiness_probe']['scheme'] = 'HTTPS'
    elif mode == 'no_http_probe': container['readiness_probe'] = None
    elif mode == 'missing_field': container.pop('readiness_probe')
    else: container['readiness_probe'].pop('port')
    state['diagnosis'] = diagnosis(state, 'readiness_probe_error').model_dump()
    assert 'patch_readiness_probe' not in get_allowed_remediation_actions(state)


@pytest.mark.parametrize('mode', ['scheme', 'no_http_probe', 'missing_field'])
def test_business_pass_does_not_override_probe_mismatch_or_unknown(setup, mode):
    original, bundle, _, _, verifier = setup
    container = bundle['deployments']['order-service']['containers'][0]
    if mode == 'scheme': container['readiness_probe']['scheme'] = 'HTTPS'
    elif mode == 'no_http_probe': container['readiness_probe'] = None
    else: container.pop('readiness_probe')
    result = verifier.verify(original)
    assert result.status == 'failed' and result.business_status == 'passed'
    assert not next(c for c in result.checks if c.name == 'registered_readiness_configuration').passed


@pytest.mark.parametrize('value', [0, -1, True, '2', 2.0])
def test_invalid_registered_replica_counts_are_rejected(state, value):
    raw = copy.deepcopy(state['service_profile']['profile'])
    raw['expected_replicas'] = value
    with pytest.raises(ValidationError): ServiceProfile.model_validate(raw)


def test_legacy_profile_digest_and_frozen_evidence_remain_readable():
    root = Path(__file__).resolve().parents[3]
    fixture = json.loads((root / 'evals/cases/v02-stage5/normal.json').read_text())
    legacy_state = fixture['state']
    raw = legacy_state['service_profile']['profile']
    assert 'expected_replicas' not in raw
    profile = ServiceProfile.model_validate(raw)
    old_payload = profile.model_dump(mode='json')
    old_payload.pop('expected_replicas')
    old_digest = hashlib.sha256(json.dumps(old_payload, sort_keys=True, separators=(',', ':'),
                                           ensure_ascii=False).encode()).hexdigest()
    assert profile_digest(profile) == old_digest == legacy_state['service_profile']['digest']
    assert matched_profile(legacy_state) == profile
    assert diagnostic_facts(legacy_state)['resource_status'] == 'ready'


def test_replica_contract_is_part_of_profile_digest(state):
    profile = matched_profile(state)
    changed = profile.model_copy(update={'expected_replicas': 1})
    assert profile_digest(profile) != profile_digest(changed)
