"""无需集群或真实模型，验证错误原文、重试、隔离和 API 返回。"""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import json
from backend.app.llm.debug_capture import ObservedService, observe_node, record_response
from backend.app.api.schemas import IncidentStatusResponse


def make_node(service):
    proxy = ObservedService(service)
    @observe_node('diagnosis')
    def node(state):
        try:
            proxy.diagnose(state)
        except Exception as exc:
            return {'phase': 'diagnosis_failed', 'errors': [{'stage': 'diagnose_incident', 'code': 'PARSE', 'message': str(exc)}]}
        return {'phase': 'diagnosis_completed'}
    return node


class ParseFailure:
    def diagnose(self, state):
        record_response({'raw': SimpleNamespace(content='{bad json', usage_metadata={'output_tokens': 3}), 'parsing_error': ValueError('invalid JSON'), 'parsed': None})
        raise ValueError('structured diagnosis parsing failed')


def test_parsing_failure_preserves_raw_and_error():
    output = make_node(ParseFailure())({})
    call = output['llm_debug']['diagnosis']['calls'][0]
    assert call['raw_output'] == '{bad json'
    assert call['parsing_error']['message'] == 'invalid JSON'
    assert call['usage']['output_tokens'] == 3
    assert call['node_errors'][0]['code'] == 'PARSE'
    json.dumps(output)


def test_timeout_has_no_invented_raw():
    class Timeout:
        def diagnose(self, state):
            raise TimeoutError('timed out')
    call = make_node(Timeout())({})['llm_debug']['diagnosis']['calls'][0]
    assert call['raw_available'] is False
    assert call['exception']['type'] == 'TimeoutError'


class Success:
    def diagnose(self, state):
        return SimpleNamespace(diagnosis={'id': state.get('id')}, model_name='fake', usage={'output_tokens': 2})


def test_retry_preserves_both_calls_and_rejection():
    proxy = ObservedService(Success())
    @observe_node('diagnosis')
    def node(state):
        proxy.diagnose({'id': 'first'})
        proxy.diagnose({'id': 'second', 'diagnosis_validation_feedback': 'missing evidence'})
        return {'phase': 'diagnosis_failed', 'diagnosis_retry_count': 1, 'errors': [{'code': 'REJECTED'}]}
    result = node({})['llm_debug']['diagnosis']
    assert len(result['calls']) == 2
    assert result['calls'][0]['validation_error'] == 'missing evidence'
    assert result['calls'][1]['parsed_output']['id'] == 'second'
    assert result['calls'][1]['node_errors'][0]['code'] == 'REJECTED'
    assert result['retry_count'] == 1


def test_concurrent_incidents_do_not_mix():
    node = make_node(Success())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(node, [{'id': n} for n in range(20)]))
    for n, output in enumerate(results):
        calls = output['llm_debug']['diagnosis']['calls']
        assert len(calls) == 1
        assert calls[0]['parsed_output']['id'] == n


def test_api_keeps_debug_on_failed_event():
    state = make_node(ParseFailure())({})
    output = IncidentStatusResponse.from_state(incident_id='test', thread_id='test', state=state, waiting_for_approval=False).model_dump(mode='json')
    assert output['llm_debug'] == state['llm_debug']
    assert output['diagnosis'] is None


def test_planner_failure_keeps_diagnosis_and_no_valid_plan():
    class Planner:
        def plan(self, state):
            return SimpleNamespace(plan={'action': 'bad'}, model_name='fake', usage={})
    proxy = ObservedService(Planner())
    @observe_node('remediation')
    def node(state):
        proxy.plan(state)
        return {'phase': 'remediation_failed', 'errors': [{'code': 'INVALID_REMEDIATION_PLAN'}]}
    output = node({'llm_debug': {'diagnosis': {'calls': []}}})
    assert 'diagnosis' in output['llm_debug']
    assert output['llm_debug']['remediation']['calls'][0]['parsed_output']['action'] == 'bad'
    assert output.get('remediation_plan') is None
