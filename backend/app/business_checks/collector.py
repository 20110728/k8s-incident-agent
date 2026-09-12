# 后端业务检查适配：通过 Kubernetes API 访问集群内检查器，验证返回结果的绑定关系。
# 真正的业务请求由检查器通过目标 Service 发出，后端无需解析集群 DNS。

import json
import time
from uuid import uuid4

from backend.app.business_checks.protocol import (
    CHECKER_VERSION, PROTOCOL_VERSION, decode_json, validate_target,
)
from backend.app.service_profiles.registry import (
    ProfileUnavailable, collect_profile, load_profile, matched_profile, profile_digest,
)

PROBE_NAMESPACE = 'agent-demo'
PROBE_SERVICE = 'incident-agent-business-probe:80'
MAX_RESULT_BYTES = 16384


def build_target(profile, check, service):
    if (service.get('namespace'), service.get('name')) != (profile.namespace, profile.service_name):
        raise ValueError('SERVICE_ASSOCIATION_MISMATCH')
    if service.get('service_type') != 'ClusterIP' or not service.get('cluster_ip'):
        raise ValueError('ONLY_NON_HEADLESS_CLUSTERIP_SUPPORTED')
    ports = [p for p in service.get('ports', []) if p.get('protocol', 'TCP') == 'TCP'
             and (p.get('port') == check.port if type(check.port) is int else p.get('name') == check.port)]
    if len(ports) != 1:
        raise ValueError('REGISTERED_SERVICE_PORT_NOT_FOUND')
    target = {'namespace': profile.namespace, 'service_name': profile.service_name,
              'profile_digest': profile_digest(profile), 'cluster_ip': service['cluster_ip'],
              'port': ports[0]['port'], 'check': check.model_dump(mode='json')}
    return validate_target(target)


def unavailable(check_id, code, status='unknown'):
    return {'check_id': check_id, 'status': status, 'error_code': code,
            'scope': 'cluster_service_http', 'http_status': None,
            'content_matches': None, 'request_id': None}


def call_probe(clients, target, timeout):
    nonce = uuid4().hex
    path = f"checks/{target['profile_digest']}/{target['check']['check_id']}/{nonce}"
    response = clients.core.connect_get_namespaced_service_proxy_with_path(
        name=PROBE_SERVICE, namespace=PROBE_NAMESPACE, path=path,
        _preload_content=False, _request_timeout=(3, timeout))
    try:
        if response.status != 200:
            raise ValueError('PROBE_HTTP_ERROR')
        raw = response.read(MAX_RESULT_BYTES + 1)
        if len(raw) > MAX_RESULT_BYTES:
            raise ValueError('PROBE_RESPONSE_TOO_LARGE')
        result = decode_json(raw)
    finally:
        response.close()
        response.release_conn()
    if (not isinstance(result, dict) or result.get('protocol_version') != PROTOCOL_VERSION
            or result.get('checker_version') != CHECKER_VERSION
            or result.get('request_id') != nonce
            or json.dumps(result.get('target'), sort_keys=True) != json.dumps(target, sort_keys=True)
            or result.get('scope') != 'cluster_service_http'
            or result.get('status') not in {'passed', 'failed', 'unknown'}):
        raise ValueError('PROBE_RESULT_BINDING_INVALID')
    if result['status'] == 'passed' and (
            result.get('http_status') != target['check']['expected_status']
            or result.get('content_matches') is not True or result.get('error_code') is not None
            or not isinstance(result.get('observed_fields'), dict)
            or any(key not in result['observed_fields']
                   or type(result['observed_fields'][key]) is not type(value)
                   or result['observed_fields'][key] != value
                   for key, value in target['check']['expected_json_subset'].items())):
        raise ValueError('PROBE_PASS_RESULT_INVALID')
    result['check_id'] = target['check']['check_id']
    return result


def collect_business_checks(clients, bundle):
    from backend.app.agent.collector_adapter import normalize_evidence
    from backend.app.tools.service_tools import get_service

    snapshot = bundle.get('service_profile')
    state = {'request': {'namespace': bundle['namespace'], 'service_name': bundle['service_name']},
             'service_profile': snapshot, 'evidence': normalize_evidence(incident_id='profile-check', bundle=bundle)}
    try:
        profile = matched_profile(state)
        current = load_profile(profile.namespace, profile.service_name)
        if profile_digest(current) != profile_digest(profile):
            raise ProfileUnavailable('PROFILE_CHANGED_BEFORE_CHECK')
    except (ProfileUnavailable, ValueError):
        return [unavailable(None, 'SERVICE_PROFILE_NOT_MATCHED', 'skipped')]
    if not profile.business_checks:
        return [unavailable(None, 'NO_REGISTERED_BUSINESS_CHECKS', 'skipped')]
    results = []
    deadline = time.monotonic() + 25
    for check in profile.business_checks:
        remaining = deadline - time.monotonic()
        if remaining < check.timeout_seconds + 6:
            results.append(unavailable(check.check_id, 'COLLECTION_CHECK_BUDGET_EXHAUSTED', 'skipped'))
            continue
        try:
            target = build_target(profile, check, bundle.get('service') or {})
            results.append(call_probe(clients, target, check.timeout_seconds + 3))
        except Exception as error:
            code = ('PROBE_ACCESS_DENIED' if getattr(error, 'status', None) in (401, 403)
                    else 'PROBE_UNAVAILABLE_OR_INVALID')
            result = unavailable(check.check_id, code)
            result['error_type'] = type(error).__name__
            result['probe_http_status'] = getattr(error, 'status', None)
            # Application bodies and API error strings are not promoted to policy values.
            if isinstance(error, ValueError): result['detail'] = str(error)[:160]
            results.append(result)
    # Do not label a probe result current if the contract or target changed mid-check.
    try:
        fresh_service = get_service(clients, profile.namespace, profile.service_name).model_dump(mode='json')
        if fresh_service != bundle.get('service'):
            raise ValueError('SERVICE_CHANGED_DURING_CHECK')
        fresh_snapshot = collect_profile(clients, {'namespace': profile.namespace, 'service_name': profile.service_name})
        if fresh_snapshot != snapshot:
            raise ValueError('APPLICATION_OR_PROFILE_CHANGED_DURING_CHECK')
        for result in results:
            if result.get('target') is not None:
                check = next(c for c in profile.business_checks if c.check_id == result['check_id'])
                if build_target(profile, check, fresh_service) != result['target']:
                    raise ValueError('SERVICE_CHANGED_DURING_CHECK')
    except Exception:
        for result in results:
            if result.get('target') is not None and result['status'] != 'skipped':
                result['observed_status'] = result['status']
                result.update(status='unknown', error_code='CHECK_OBSERVATION_NO_LONGER_CURRENT')
    return results
