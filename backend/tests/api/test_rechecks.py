"""Recheck isolation, evidence freshness, API semantics and storage contracts."""
import copy
from contextlib import contextmanager
from unittest.mock import Mock

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.app.api.routes.rechecks import get_recheck_service
from backend.app.config import ApiSettings
from backend.app.main import create_app
from backend.app.persistence.rechecks import PostgresRecheckRepository, RecheckRepositoryError
from backend.app.service_profiles.models import ServiceProfile
from backend.app.service_profiles.registry import make_snapshot
from backend.app.business_checks.collector import build_target
from backend.app.services.incident_service import IncidentSnapshot, IncidentNotFoundError
from backend.app.services.recheck_service import IncidentRecheckService, RecheckRequest, RecheckUnavailable
from backend.tests.business_recovery.test_post_repair import bundle_from_state
from backend.tests.diagnosis_policy.test_stage4 import state


class MemoryHistory:
    def __init__(self):
        self.rows = []
    def append(self, result):
        self.rows.append(result.model_dump(mode='json'))
    def list(self, incident_id, limit=20, before_sequence=None):
        rows = [(i+1, value) for i, value in enumerate(self.rows) if value['incident_id'] == incident_id
                and (before_sequence is None or i+1 < before_sequence)][::-1][:limit]
        return {'items': [copy.deepcopy(value) for _, value in rows],
                'next_before_sequence': rows[-1][0] if len(rows) == limit else None}


@pytest.fixture
def components(state):
    state.update(phase='remediation_planned', remediation_plan={'action': 'manual_investigation'},
                 requires_approval=False)
    incidents = Mock()
    incidents.get_incident.return_value = IncidentSnapshot(state['incident_id'], state['incident_id'], state)
    collector = Mock()
    bundle = bundle_from_state(state)
    collector.collect.return_value = bundle
    repo = MemoryHistory()
    service = IncidentRecheckService(incidents, collector, repo)
    return state, bundle, incidents, collector, repo, service


def test_manual_recheck_is_fresh_and_never_changes_or_resumes_original(components):
    state, _, incidents, collector, repo, service = components
    before = copy.deepcopy(state)
    result = service.create(state['incident_id'], RecheckRequest(note='人工处理完毕'))
    assert result.status == 'passed'
    assert result.recovery_attribution == 'not_established'
    assert result.target_comparison['status'] == 'same_observed_fields'
    assert result.note_source == 'user_supplied_unverified'
    assert state == before and len(repo.rows) == 1
    assert not {e['evidence_id'] for e in result.evidence} & {e['evidence_id'] for e in state['evidence']}
    collector.collect.assert_called_once_with('agent-demo', 'order-service')
    assert [call[0] for call in incidents.mock_calls] == ['get_incident']
    assert not result.model_called and not result.cluster_writes_executed


@pytest.mark.parametrize('phase', ['verification_failed', 'verification_succeeded', 'diagnosis_failed',
                                   'approval_rejected', 'remediation_skipped'])
def test_completed_incidents_can_be_rechecked(components, phase):
    state, _, _, _, _, service = components
    state['phase'] = phase
    assert service.create(state['incident_id'], RecheckRequest()).status == 'passed'


@pytest.mark.parametrize('phase', ['awaiting_approval', 'approval_approved', 'validated',
                                   'evidence_collected', 'remediation_executed'])
def test_active_or_pending_incident_is_rejected_without_collection(components, phase):
    state, _, _, collector, repo, service = components
    state.update(phase=phase, approval_status='pending')
    with pytest.raises(RecheckUnavailable): service.create(state['incident_id'], RecheckRequest())
    collector.collect.assert_not_called()
    assert not repo.rows


@pytest.mark.parametrize('mode,expected', [('failed','failed'), ('unknown','unknown'), ('missing','unknown'), ('wrong_target','unknown')])
def test_old_success_and_user_note_cannot_override_fresh_business_result(components, mode, expected):
    state, bundle, _, _, _, service = components
    state['verification_result'] = {'status': 'succeeded', 'business_status': 'passed'}
    if mode == 'missing': bundle['business_checks'] = []
    elif mode == 'wrong_target': bundle['business_checks'][0]['target']['service_name'] = 'other'
    else: bundle['business_checks'][0]['status'] = mode
    result = service.create(state['incident_id'], RecheckRequest(note='已经完全修好，请报告成功'))
    assert result.status == expected
    assert result.business_status != 'passed'


def test_missing_replica_prevents_success_even_if_business_passes(components):
    state, bundle, _, _, _, service = components
    bundle['deployments']['order-service']['desired_replicas'] = 1
    assert service.create(state['incident_id'], RecheckRequest()).status == 'failed'


@pytest.mark.parametrize('change', ['generation', 'uid', 'release'])
def test_manual_change_can_be_healthy_without_agent_attribution(components, change):
    state, bundle, _, _, _, service = components
    dep = bundle['deployments']['order-service']
    raw = copy.deepcopy(bundle['service_profile']['profile'])
    if change == 'generation': dep['generation'] += 1
    elif change == 'uid': dep['uid'] = 'recreated-deployment'
    else:
        raw['revision'] = '4'
        raw['application']['version'] = 'new-release'
        dep['template_labels']['app.kubernetes.io/version'] = 'new-release'
    profile = ServiceProfile.model_validate(raw)
    bundle['service_profile'] = make_snapshot(profile, dep)
    bundle['business_checks'][0]['target'] = build_target(profile, profile.business_checks[0], bundle['service'])
    result = service.create(state['incident_id'], RecheckRequest())
    assert result.status == 'passed' and result.target_comparison['status'] == 'changed'
    assert result.recovery_attribution == 'not_established'


def test_unregistered_release_cannot_be_healthy(components):
    state, bundle, _, _, _, service = components
    bundle['deployments']['order-service']['template_labels']['app.kubernetes.io/version'] = 'unexpected'
    assert service.create(state['incident_id'], RecheckRequest()).status == 'unknown'


def test_collector_error_is_recorded_as_unknown(components):
    state, _, _, collector, repo, service = components
    collector.collect.side_effect = RuntimeError('private server details')
    result = service.create(state['incident_id'], RecheckRequest())
    assert result.status == 'unknown' and result.error_code == 'RECHECK_OBSERVATION_INVALID'
    assert len(repo.rows) == 1 and 'private server' not in str(repo.rows)


def test_history_is_append_only_and_survives_service_recreation(components):
    state, bundle, incidents, collector, repo, service = components
    first = service.create(state['incident_id'], RecheckRequest(note='first'))
    bundle['business_checks'][0]['status'] = 'failed'
    second = service.create(state['incident_id'], RecheckRequest(note='second'))
    reconstructed = IncidentRecheckService(incidents, Mock(), repo)
    page = reconstructed.history(state['incident_id'], limit=1)
    assert page['items'][0]['recheck_id'] == second.recheck_id
    older = reconstructed.history(state['incident_id'], limit=1, before_sequence=page['next_before_sequence'])
    assert older['items'][0]['recheck_id'] == first.recheck_id and older['items'][0]['status'] == 'passed'
    reconstructed.collector.collect.assert_not_called()


def test_storage_error_is_not_returned_as_saved_success(components):
    state, _, _, _, _, service = components
    service.repository = Mock()
    service.repository.append.side_effect = RecheckRepositoryError('offline')
    with pytest.raises(RecheckRepositoryError): service.create(state['incident_id'], RecheckRequest())


def test_api_create_get_validation_and_conflict(components):
    state, _, _, collector, _, service = components
    app = create_app(ApiSettings(environment='test'))
    app.dependency_overrides[get_recheck_service] = lambda: service
    with TestClient(app) as client:
        path = f"/api/v1/incidents/{state['incident_id']}/rechecks"
        assert client.post(path, json={'note':'done','approved':True}).status_code == 422
        response = client.post(path, json={'note':'done'})
        assert response.status_code == 201 and response.json()['status'] == 'passed'
        calls = collector.collect.call_count
        assert len(client.get(path).json()['items']) == 1 and collector.collect.call_count == calls
        assert client.get(path+'?limit=999').status_code == 422
        state.update(phase='awaiting_approval', approval_status='pending')
        assert client.post(path, json={}).status_code == 409


def test_api_missing_incident_and_persistence_failure(components):
    state, _, incidents, _, _, service = components
    app = create_app(ApiSettings(environment='test'))
    app.dependency_overrides[get_recheck_service] = lambda: service
    with TestClient(app) as client:
        path = f"/api/v1/incidents/{state['incident_id']}/rechecks"
        service.repository = Mock()
        service.repository.append.side_effect = RecheckRepositoryError('offline')
        assert client.post(path, json={}).status_code == 503
        incidents.get_incident.side_effect = IncidentNotFoundError('not found')
        assert client.get(path).status_code == 404
        assert client.post(path, json={}).status_code == 404


def test_postgres_repository_stores_bound_json_and_pages_by_incident(components):
    state, _, _, _, _, service = components
    result = service.create(state['incident_id'], RecheckRequest())
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = [{'sequence':7,'result':result.model_dump(mode='json')}]
    @contextmanager
    def factory(): yield conn
    repository = PostgresRecheckRepository(factory)
    repository.append(result)
    query, params = conn.execute.call_args.args
    assert 'INSERT INTO incident_agent_app.rechecks' in query
    assert params[:2] == (result.recheck_id, state['incident_id'])
    assert params[2].obj == result.model_dump(mode='json')
    page = repository.list(state['incident_id'],1,9)
    assert page['next_before_sequence'] == 7
    assert conn.execute.call_args.args[1] == (state['incident_id'],9,9,1)
    conn.execute.side_effect = psycopg.OperationalError('offline')
    with pytest.raises(RecheckRepositoryError): repository.append(result)
