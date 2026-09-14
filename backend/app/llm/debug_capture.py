"""记录模型调用及节点校验结果；只用于调试，不参与诊断或执行决策。"""
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from time import monotonic

_current = ContextVar('incident_debug_call', default=None)
_calls = ContextVar('incident_debug_calls', default=None)


def _value(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    if isinstance(value, dict):
        return {str(k): _value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def record_response(response):
    """在解析前记录服务商返回内容，不记录提示词、请求头和客户端配置。"""
    record = _current.get()
    if record is None:
        return
    raw = response.get('raw')
    record['raw_available'] = raw is not None
    if raw is not None:
        record['raw_output'] = _value(getattr(raw, 'content', None))
        record['tool_calls'] = _value(getattr(raw, 'tool_calls', []))
        record['invalid_tool_calls'] = _value(getattr(raw, 'invalid_tool_calls', []))
        record['usage'] = _value(getattr(raw, 'usage_metadata', None))
        metadata = getattr(raw, 'response_metadata', {}) or {}
        record['response_metadata'] = {
            key: _value(metadata[key]) for key in
            ('model_name', 'finish_reason', 'token_usage') if key in metadata
        }
    record['parsed_output'] = _value(response.get('parsed'))
    error = response.get('parsing_error')
    record['parsing_error'] = None if error is None else {
        'type': type(error).__name__, 'message': str(error)
    }


class ObservedService:
    """每次服务调用单独留档；ContextVar 隔离并发事件与重试。"""
    def __init__(self, service):
        self.service = service

    def _invoke(self, method, state):
        records = _calls.get()
        if records is None:
            return getattr(self.service, method)(state)
        feedback = state.get('diagnosis_validation_feedback')
        if records and feedback:
            records[-1]['validation_error'] = feedback
        record = {
            'attempt': len(records) + 1,
            'started_at': datetime.now(timezone.utc).isoformat(),
            'validation_feedback': feedback,
            'raw_available': False,
            'raw_output': None,
        }
        records.append(record)
        token = _current.set(record)
        started = monotonic()
        try:
            result = getattr(self.service, method)(state)
            output = result.diagnosis if method == 'diagnose' else result.plan
            record['parsed_output'] = _value(output)
            record['model'] = result.model_name
            record['usage'] = _value(result.usage)
            record['call_status'] = 'returned'
            return result
        except Exception as exc:
            record['call_status'] = 'exception'
            record['exception'] = {'type': type(exc).__name__, 'message': str(exc)}
            raise
        finally:
            record['elapsed_ms'] = round((monotonic() - started) * 1000)
            _current.reset(token)

    def diagnose(self, state):
        return self._invoke('diagnose', state)

    def plan(self, state):
        return self._invoke('plan', state)


def observe_node(stage):
    def decorate(function):
        @wraps(function)
        def run(state):
            records = []
            token = _calls.set(records)
            try:
                output = function(state)
                errors = _value(output.get('errors', []))
                if records and errors:
                    records[-1]['node_errors'] = errors
                debug = dict(state.get('llm_debug') or {})
                debug[stage] = {
                    'phase': output.get('phase'),
                    'calls': records,
                    'errors': errors,
                    'retry_count': output.get('diagnosis_retry_count', 0),
                }
                output['llm_debug'] = debug
                return output
            finally:
                _calls.reset(token)
        return run
    return decorate
