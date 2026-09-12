import copy
import io
import json
import socket
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

import pytest
import urllib3
from kubernetes.client.exceptions import ApiException

from backend.app.business_checks import collector, protocol, server
from backend.app.agent.collector_adapter import normalize_evidence
from backend.app.llm.context_builder import build_diagnosis_context
from backend.app.service_profiles.registry import load_profile, make_snapshot


@contextmanager
def serving(http_server):
    thread = Thread(target=http_server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{http_server.server_port}'
    finally:
        http_server.shutdown(); http_server.server_close(); thread.join(timeout=2)


@pytest.fixture
def registered(monkeypatch):
    monkeypatch.delenv('INCIDENT_AGENT_SERVICE_PROFILE_DIR', raising=False)
    profile = load_profile('agent-demo', 'order-service')
    service = dict(namespace='agent-demo', name='order-service', service_type='ClusterIP',
                   cluster_ip='10.96.23.145', selector={'app':'order-service'},
                   ports=[dict(name='http', port=80, target_port='http', protocol='TCP')])
    target = collector.build_target(profile, profile.business_checks[0], service)
    deployment = dict(namespace='agent-demo', name='order-service', uid='uid-1', generation=2,
                      template_labels={'app':'order-service', 'app.kubernetes.io/version':profile.application.version},
                      containers=[dict(name='order-service', image=profile.application.images['order-service'])])
    bundle = dict(namespace='agent-demo', service_name='order-service', service=service,
                  deployments={'order-service':deployment}, service_profile=make_snapshot(profile, deployment))
    return target, bundle


@contextmanager
def target_http(monkeypatch, target, code, body):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            calls.append((self.path, self.headers['Host']))
            self.send_response(code)
            self.send_header('Location', 'http://169.254.169.254/credentials')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
    real_dns = socket.getaddrinfo
    real_http = protocol.http.client.HTTPConnection
    with serving(ThreadingHTTPServer(('127.0.0.1', 0), Handler)) as endpoint:
        port = int(endpoint.rsplit(':', 1)[1])
        def dns(host, *args, **kwargs):
            if host == protocol.hostname(target):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (target['cluster_ip'], 80))]
            return real_dns(host, *args, **kwargs)
        def connection(host, dest_port, **kwargs):
            if host == target['cluster_ip']:
                return real_http('127.0.0.1', port, **kwargs)
            return real_http(host, dest_port, **kwargs)
        with monkeypatch.context() as context:
            context.setattr(protocol.socket, 'getaddrinfo', dns)
            context.setattr(protocol.http.client, 'HTTPConnection', connection)
            yield calls


@pytest.mark.parametrize('code,body,status,error', [
    (200, b'{"order_id":"demo-001","status":"confirmed","dependency_status":"available"}', 'passed', None),
    (500, b'{"error":"application_error"}', 'failed', 'HTTP_STATUS_MISMATCH'),
    (200, b'{"order_id":"wrong-order","status":"cancelled"}', 'failed', 'JSON_CONTENT_MISMATCH'),
    (200, b'not-json', 'failed', 'INVALID_JSON'),
    (200, b'{"order_id":"wrong","order_id":"demo-001"}', 'failed', 'INVALID_JSON'),
    (302, b'{}', 'failed', 'HTTP_STATUS_MISMATCH'),
    (200, b'x' * 16385, 'unknown', 'RESPONSE_TOO_LARGE'),
])
def test_target_http_results(monkeypatch, registered, code, body, status, error):
    target, _ = registered
    with target_http(monkeypatch, target, code, body) as calls:
        result = protocol.perform_check(target, 'a' * 32)
    assert result['status'] == status and result['error_code'] == error
    assert calls == [('/api/orders/demo-001', 'order-service.agent-demo.svc.cluster.local:80')]


@pytest.mark.parametrize('change', [
    lambda t: t.update(cluster_ip='127.0.0.1'),
    lambda t: t.update(cluster_ip='169.254.169.254'),
    lambda t: t.update(cluster_ip='metadata.example'),
    lambda t: t.update(service_name='order/../other'),
    lambda t: t['check'].update(path='//evil.example/path'),
    lambda t: t['check'].update(path='/x?target=http://evil.example'),
    lambda t: t['check'].update(method='POST'),
    lambda t: t['check'].update(follow_redirects=True),
    lambda t: t['check'].update(timeout_seconds=100),
    lambda t: t['check'].update(scheme='HTTPS'),
])
def test_target_constraints(registered, change):
    target, _ = registered; change(target)
    with pytest.raises(ValueError): protocol.validate_target(target)


def test_dns_mismatch_never_connects(monkeypatch, registered):
    target, _ = registered
    monkeypatch.setattr(protocol.socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254',80))])
    connect = Mock(); monkeypatch.setattr(protocol.http.client, 'HTTPConnection', connect)
    assert protocol.perform_check(target, 'a'*32)['error_code'] == 'SERVICE_DNS_MISMATCH'
    connect.assert_not_called()


@pytest.mark.parametrize('error,code', [(socket.gaierror(), 'DNS_ERROR'), (TimeoutError(), 'CHECK_TIMEOUT'), (ConnectionRefusedError(), 'CONNECTION_ERROR')])
def test_transport_failures_are_unknown(monkeypatch, registered, error, code):
    target, _ = registered
    monkeypatch.setattr(protocol.socket, 'getaddrinfo', Mock(side_effect=error))
    result = protocol.perform_check(target, 'a'*32)
    assert result['status'] == 'unknown' and result['error_code'] == code
    assert result['http_status'] is None


def test_json_assertions_are_type_strict(monkeypatch, registered):
    target, _ = registered; target['check']['expected_json_subset'] = {'ok':True}
    with target_http(monkeypatch, target, 200, b'{"ok":1}'):
        assert protocol.perform_check(target, 'a'*32)['status'] == 'failed'


def fetch(url, data=None):
    try: response = build_opener(ProxyHandler({})).open(url, data=data, timeout=4)
    except HTTPError as e: response = e
    with response: return response.status, json.loads(response.read())


def test_checker_rejects_unregistered_targets_and_arbitrary_requests(tmp_path, registered):
    target, _ = registered; config = tmp_path/'targets.json'
    config.write_text(json.dumps({'targets':[target]})); run = Mock()
    path = f"/checks/{target['profile_digest']}/{target['check']['check_id']}/{'a'*32}"
    with serving(server.make_server(config, '127.0.0.1', 0, run_check=run)) as base:
        assert fetch(base+'/livez')[0] == 200
        assert fetch(base+'/readyz')[0] == 200
        assert fetch(base+'/check?url=http://evil.example')[0] == 400
        assert fetch(base+path.replace(target['profile_digest'], 'b'*64))[0] == 409
        assert fetch(base+path, data=b'{}')[0] == 405
    run.assert_not_called()


class Response:
    status = 200
    def __init__(self, data): self.data = io.BytesIO(json.dumps(data).encode())
    def read(self, n): return self.data.read(n)
    def close(self): pass
    def release_conn(self): pass


def test_backend_does_not_accept_stale_nonce(registered):
    target, _ = registered; result = protocol.result_base(target, 'b'*32)
    clients = SimpleNamespace(core=SimpleNamespace(connect_get_namespaced_service_proxy_with_path=Mock(return_value=Response(result))))
    with pytest.raises(ValueError, match='BINDING_INVALID'): collector.call_probe(clients, target, 4)
    kwargs = clients.core.connect_get_namespaced_service_proxy_with_path.call_args.kwargs
    assert kwargs['name'] == 'incident-agent-business-probe:80'
    assert kwargs['namespace'] == 'agent-demo'
    assert 'order-service' not in kwargs['name']


def patch_refresh(monkeypatch, bundle):
    from backend.app.schemas.kubernetes import ServiceInfo
    monkeypatch.setattr('backend.app.tools.service_tools.get_service', lambda *a: ServiceInfo.model_validate(bundle['service']))
    monkeypatch.setattr(collector, 'collect_profile', lambda *a: bundle['service_profile'])


@pytest.mark.parametrize('status', [403, 404, 503])
def test_probe_failure_not_reported_as_business_success(monkeypatch, registered, status):
    target, bundle = registered; patch_refresh(monkeypatch, bundle)
    proxy = Mock(side_effect=ApiException(status=status))
    result = collector.collect_business_checks(SimpleNamespace(core=SimpleNamespace(connect_get_namespaced_service_proxy_with_path=proxy)), bundle)
    assert result[0]['status'] == 'unknown'
    assert result[0]['error_code'] == ('PROBE_ACCESS_DENIED' if status == 403 else 'PROBE_UNAVAILABLE_OR_INVALID')


def test_mismatched_profile_skips_probe(registered):
    _, bundle = registered; bundle['service_profile']['status'] = 'mismatch'
    proxy = Mock()
    results = collector.collect_business_checks(SimpleNamespace(core=SimpleNamespace(connect_get_namespaced_service_proxy_with_path=proxy)), bundle)
    assert results[0]['status'] == 'skipped'; proxy.assert_not_called()


def test_real_http_checker_result_reaches_evidence(monkeypatch, registered, tmp_path):
    target, bundle = registered; patch_refresh(monkeypatch, bundle)
    config = tmp_path/'targets.json'; config.write_text(json.dumps({'targets':[target]}))
    good = b'{"order_id":"demo-001","status":"confirmed","dependency_status":"available"}'
    with target_http(monkeypatch, target, 200, good):
        with serving(server.make_server(config, '127.0.0.1', 0, run_check=protocol.perform_check)) as base:
            pool = urllib3.PoolManager()
            def proxy(**kwargs):
                return pool.request('GET', base+'/'+kwargs['path'], preload_content=False)
            clients = SimpleNamespace(core=SimpleNamespace(connect_get_namespaced_service_proxy_with_path=proxy))
            bundle['business_checks'] = collector.collect_business_checks(clients, bundle)
            pool.clear()
    evidence = normalize_evidence(incident_id='stage3-check', bundle=bundle)
    assert evidence[-1]['resource_type'] == 'BusinessCheck'
    assert evidence[-1]['source'] == 'cluster_http_probe'
    assert evidence[-1]['data']['status'] == 'passed'
    context = json.loads(build_diagnosis_context({'evidence':evidence}))
    assert context['evidence'][0]['resource_type'] == 'BusinessCheck'
    assert evidence[-1]['evidence_id'] in context['available_evidence_ids']


def test_application_change_during_check_invalidates_success(monkeypatch, registered):
    target, bundle = registered; patch_refresh(monkeypatch, bundle)
    monkeypatch.setattr(collector, 'call_probe', lambda *a: {**protocol.result_base(target, 'a'*32), 'status':'passed', 'check_id':target['check']['check_id']})
    fresh = copy.deepcopy(bundle['service_profile']); fresh['deployment_generation'] += 1
    monkeypatch.setattr(collector, 'collect_profile', lambda *a: fresh)
    result = collector.collect_business_checks(object(), bundle)[0]
    assert result['status'] == 'unknown' and result['observed_status'] == 'passed'


@pytest.mark.parametrize('service_type,ip,ports', [('ExternalName','10.96.1.1',[{'port':80}]), ('ClusterIP','None',[{'port':80}]), ('ClusterIP','10.96.1.1',[{'port':81}])])
def test_external_headless_or_unregistered_port_refused(registered, service_type, ip, ports):
    _, bundle = registered; service = bundle['service']; service.update(service_type=service_type, cluster_ip=ip, ports=ports)
    p = load_profile('agent-demo','order-service')
    with pytest.raises(ValueError): collector.build_target(p,p.business_checks[0],service)


def slow_child(send, target, nonce):
    import time
    time.sleep(10)


def test_hard_deadline_terminates_worker(monkeypatch, registered):
    target, _ = registered; target['check']['timeout_seconds'] = 1
    monkeypatch.setattr(server, '_child', slow_child)
    result = server.bounded_check(target, 'a'*32)
    assert result['status'] == 'unknown' and result['error_code'] == 'CHECK_TIMEOUT'


def test_actual_spawn_worker_starts(registered):
    target, _ = registered; target['check']['timeout_seconds'] = 1
    target['service_name'] = 'absent-stage3-test-service'
    result = server.bounded_check(target, 'a'*32)
    assert result['status'] == 'unknown'
    assert result['error_code'] in {'DNS_ERROR','CHECK_TIMEOUT','SERVICE_DNS_MISMATCH'}


def test_named_service_port_is_resolved(registered):
    _, bundle = registered; p = load_profile('agent-demo', 'order-service')
    check = p.business_checks[0].model_copy(update={'port':'http'})
    target = collector.build_target(p, check, bundle['service'])
    assert target['port'] == 80 and target['check']['port'] == 'http'


def test_inconsistent_pass_is_rejected(registered):
    target, _ = registered
    def proxy(**kwargs):
        result = protocol.result_base(target, kwargs['path'].split('/')[-1])
        result.update(status='passed', http_status=200, content_matches=True, observed_fields={})
        return Response(result)
    with pytest.raises(ValueError, match='PASS_RESULT_INVALID'):
        collector.call_probe(SimpleNamespace(core=SimpleNamespace(connect_get_namespaced_service_proxy_with_path=proxy)), target, 4)


def test_probe_manifest_has_no_exec_or_cluster_credentials():
    import yaml
    root = Path(__file__).resolve().parents[3]
    docs = list(yaml.safe_load_all((root/'infra/business-probe/baseline.yaml').read_text()))
    role = next(d for d in docs if d['kind']=='Role')
    assert role['rules'][0]['resources'] == ['services/proxy']
    assert role['rules'][0]['verbs'] == ['get']
    assert set(role['rules'][0]['resourceNames']) == {'incident-agent-business-probe','incident-agent-business-probe:80'}
    podspec = next(d for d in docs if d['kind']=='Deployment')['spec']['template']['spec']
    assert podspec['automountServiceAccountToken'] is False
    assert podspec['containers'][0]['securityContext']['readOnlyRootFilesystem'] is True


def test_sdk_proxy_call_signature_and_route():
    from kubernetes.client import CoreV1Api
    api = CoreV1Api(); api.api_client.call_api = Mock(return_value='fake')
    api.connect_get_namespaced_service_proxy_with_path(name=collector.PROBE_SERVICE, namespace='agent-demo',
        path='checks/digest/id/nonce', _preload_content=False, _request_timeout=(3,6))
    args, kwargs = api.api_client.call_api.call_args
    assert args[0] == '/api/v1/namespaces/{namespace}/services/{name}/proxy/{path}'
    assert args[1] == 'GET'
    assert args[2]['name'] == 'incident-agent-business-probe:80'
