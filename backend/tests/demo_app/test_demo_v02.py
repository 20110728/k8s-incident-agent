import json
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'infra' / 'demo-app'))
from demo_app import VERSION
from demo_app.inspect import EXPECTED_STATUSES, inspect, matches
from demo_app.server import Settings, make_server
from backend.app.agent.remediation_policy import get_allowed_remediation_actions
from backend.app.service_profiles.models import ServiceProfile
from backend.app.service_profiles.registry import make_snapshot


@contextmanager
def serving(server):
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def request(base, path, data=None):
    try:
        response = build_opener(ProxyHandler({})).open(base + path, data=data, timeout=3)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


@pytest.mark.parametrize('scenario', list(EXPECTED_STATUSES))
def test_http_scenarios(scenario):
    with serving(make_server(Settings('dependency'), '127.0.0.1', 0)) as downstream:
        if scenario == 'dependency_unavailable':
            with serving(make_server(Settings('dependency'), '127.0.0.1', 0)) as stopped:
                pass
            downstream = stopped
        mode = 'normal' if scenario == 'dependency_unavailable' else scenario
        with serving(make_server(Settings('order', fault_mode=mode, dependency_url=downstream), '127.0.0.1', 0)) as order:
            result = inspect(order)
    assert [item['http_status'] for item in result] == EXPECTED_STATUSES[scenario]
    assert matches(result, scenario)
    assert result[0]['body']['version'] == VERSION
    if scenario in {'api500', 'wrong_content'}:
        assert result[1]['body']['status'] == 'ready'
        assert not matches(result, 'normal')
    if scenario == 'dependency_unavailable':
        assert result[2]['body']['error'] == 'dependency_unavailable'


def test_independent_dependency_http_service():
    with serving(make_server(Settings('dependency'), '127.0.0.1', 0)) as base:
        assert request(base, '/livez')[0] == 200
        assert request(base, '/readyz')[0] == 200
        code, body = request(base, '/api/availability/demo-001')
        assert code == 200 and body['available'] is True
        assert request(base, '/api/orders/demo-001')[0] == 404


def test_unknown_paths_and_write_methods():
    with serving(make_server(Settings('order'), '127.0.0.1', 0)) as base:
        assert request(base, '/livez')[0] == 200
        assert request(base, '/healthz')[0] == 404
        assert request(base, '/readyz?path=/livez')[0] == 404
        assert request(base, '/api/orders/demo-001', data=b'{}')[0] == 405


@pytest.mark.parametrize('code,body', [
    (503, b'{"error":"unavailable"}'), (200, b'not-json'), (200, b'[]'),
    (200, b'{"order_id":"other","available":true}'),
    (200, b'{"order_id":"demo-001","available":false}'),
    (200, b'{"order_id":"demo-001","available":1}'),
    (200, b'x' * 16385), (302, b'{}'),
])
def test_bad_dependency_response_fails_readiness_only(code, body):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            calls.append(self.path)
            self.send_response(code)
            self.send_header('Location', '/unexpected-redirect')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    with serving(ThreadingHTTPServer(('127.0.0.1', 0), Handler)) as downstream:
        with serving(make_server(Settings('order', dependency_url=downstream), '127.0.0.1', 0)) as base:
            assert request(base, '/readyz')[0] == 503
            assert request(base, '/livez')[0] == 200
    assert calls == ['/api/availability/demo-001']


@pytest.mark.parametrize('value', ['typo', '', 'shell'])
def test_unknown_mode_refuses_startup(value):
    with pytest.raises(ValueError, match='ORDER_FAULT_MODE'):
        Settings('order', fault_mode=value)


def test_active_profile_matches_manifest_and_application():
    profile = ServiceProfile.model_validate(json.loads((ROOT / 'config/service-profiles/agent-demo.order-service.json').read_text()))
    docs = list(yaml.safe_load_all((ROOT / 'infra/demo-app/baseline.yaml').read_text()))
    deployments = {d['metadata']['name']: d for d in docs if d['kind'] == 'Deployment'}
    services = {d['metadata']['name']: d for d in docs if d['kind'] == 'Service'}
    assert set(deployments) == set(services) == {'order-service', 'order-dependency'}
    assert profile.application.version == VERSION and profile.revision == '2'
    spec = deployments['order-service']['spec']
    container = spec['template']['spec']['containers'][0]
    assert container['imagePullPolicy'] == 'Never'
    assert profile.application.images == {container['name']: container['image']}
    assert profile.expected_selector == services['order-service']['spec']['selector']
    assert container['readinessProbe']['httpGet'] == {'path': '/readyz', 'port': 'http'}
    assert container['livenessProbe']['httpGet'] == {'path': '/livez', 'port': 'http'}
    assert profile.readiness_probe.path == '/readyz' and profile.liveness_probe.path == '/livez'
    assert profile.business_checks[0].path == '/api/orders/demo-001'
    assert profile.business_checks[0].expected_json_subset == {
        'order_id': 'demo-001', 'status': 'confirmed', 'dependency_status': 'available'}
    data = {'namespace': 'agent-demo', 'name': 'order-service', 'uid': 'demo-uid', 'generation': 1,
            'template_labels': spec['template']['metadata']['labels'],
            'containers': [{'name': container['name'], 'image': container['image'],
                            'readiness_probe': {'path': '/readyz', 'port': 'http', 'scheme': 'HTTP'}}]}
    snapshot = make_snapshot(profile, data)
    assert snapshot['status'] == 'matched'
    state = {'request': {'namespace': 'agent-demo', 'service_name': 'order-service'},
             'service_profile': snapshot, 'diagnosis': {'fault_category': 'readiness_probe_error'},
             'evidence': [{'resource_type': 'Deployment', 'resource_name': 'order-service', 'data': data}]}
    assert get_allowed_remediation_actions(state) == {'manual_investigation'}


def test_old_policy_fixtures_are_independent_of_current_demo_release():
    from backend.tests.service_profiles.fixtures import profile_and_deployment
    profile, data = profile_and_deployment()
    assert profile.application.version == 'day21-nginx-v1'
    assert make_snapshot(profile, data)['status'] == 'matched'
    assert profile.application.version != VERSION


def test_dependency_timeout_returns_503():
    from threading import Event
    release = Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self): release.wait(2)
    with serving(ThreadingHTTPServer(('127.0.0.1', 0), Handler)) as downstream:
        try:
            settings = Settings('order', dependency_url=downstream, dependency_timeout=0.02)
            with serving(make_server(settings, '127.0.0.1', 0)) as base:
                assert request(base, '/readyz')[0] == 503
                assert request(base, '/livez')[0] == 200
        finally:
            release.set()
