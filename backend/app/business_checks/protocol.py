# 业务检查协议与 HTTP 实现：校验登记目标、解析 Service DNS 并访问绑定的 ClusterIP。
# 结果区分断言失败与无法取得结果；HTTP 200 仍需通过 JSON 内容断言。

"""Shared, standard-library-only checker contract and HTTP implementation."""
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import time
from datetime import UTC, datetime

PROTOCOL_VERSION = 1
CHECKER_VERSION = "business-probe-v0.2.0"
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
CHECK_ID = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
NONCE = re.compile(r"^[a-f0-9]{32}$")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode_json(raw):
    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def validate_target(target):
    if not isinstance(target, dict) or set(target) != {
        'namespace', 'service_name', 'profile_digest', 'cluster_ip', 'port', 'check'}:
        raise ValueError('invalid target fields')
    if not NAME.fullmatch(target['namespace']) or not NAME.fullmatch(target['service_name']):
        raise ValueError('invalid resource name')
    if not DIGEST.fullmatch(target['profile_digest']):
        raise ValueError('invalid digest')
    address = ipaddress.ip_address(target['cluster_ip'])
    if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
        raise ValueError('unsafe Service address')
    if type(target['port']) is not int or not 1 <= target['port'] <= 65535:
        raise ValueError('invalid Service port')
    check = target['check']
    if not isinstance(check, dict) or set(check) != {
        'check_id', 'path', 'port', 'scheme', 'method', 'expected_status',
        'expected_json_subset', 'timeout_seconds', 'max_response_bytes',
        'follow_redirects', 'read_only'}:
        raise ValueError('invalid check fields')
    if not CHECK_ID.fullmatch(check['check_id']):
        raise ValueError('invalid check ID')
    path = check['path']
    if (not isinstance(path, str) or not path.startswith('/') or path.startswith('//')
            or len(path) > 300 or any(c in path for c in ('\\', '?', '#'))
            or any(c.isspace() or ord(c) < 32 for c in path)):
        raise ValueError('invalid local request path')
    if check['scheme'] != 'HTTP' or check['method'] != 'GET':
        raise ValueError('only HTTP GET is supported in stage 3')
    if check['follow_redirects'] is not False or check['read_only'] is not True:
        raise ValueError('unsafe check settings')
    for key, lower, upper in [('timeout_seconds', 1, 10), ('max_response_bytes', 1, 65536), ('expected_status', 200, 299)]:
        if type(check[key]) is not int or not lower <= check[key] <= upper:
            raise ValueError('invalid check bound')
    expected = check['expected_json_subset']
    if (not isinstance(expected, dict) or len(expected) > 20
            or any(not isinstance(k, str) or len(k) > 100 or type(v) not in (str, int, bool)
                   or (isinstance(v, str) and len(v) > 300) for k, v in expected.items())):
        raise ValueError('invalid JSON assertions')
    return target


def hostname(target):
    return f"{target['service_name']}.{target['namespace']}.svc.cluster.local"


def result_base(target, request_id):
    return {'protocol_version': PROTOCOL_VERSION, 'checker_version': CHECKER_VERSION,
            'request_id': request_id, 'target': target,
            'scope': 'cluster_service_http', 'status': 'unknown', 'error_code': None,
            'http_status': None, 'content_matches': None, 'observed_fields': {},
            'response_bytes': 0, 'body_sha256': None, 'elapsed_ms': 0,
            'checked_at': datetime.now(UTC).isoformat()}


def perform_check(target, request_id):
    validate_target(target)
    result = result_base(target, request_id)
    check = target['check']
    started = time.monotonic()
    connection = None
    try:
        # DNS must resolve to the registered ClusterIP, not an arbitrary destination.
        addresses = {str(ipaddress.ip_address(item[4][0]))
                     for item in socket.getaddrinfo(hostname(target), target['port'], type=socket.SOCK_STREAM)}
        expected_ip = str(ipaddress.ip_address(target['cluster_ip']))
        if addresses != {expected_ip}:
            result['error_code'] = 'SERVICE_DNS_MISMATCH'
            return result
        # Connect to the pinned Service VIP. This exercises Service forwarding.
        connection = http.client.HTTPConnection(expected_ip, target['port'], timeout=check['timeout_seconds'])
        connection.request('GET', check['path'], headers={'Host': f"{hostname(target)}:{target['port']}",
                                                        'Accept': 'application/json', 'Connection': 'close'})
        response = connection.getresponse()
        result['http_status'] = response.status
        body = response.read(check['max_response_bytes'] + 1)
        result['response_bytes'] = len(body)
        if len(body) > check['max_response_bytes']:
            result['error_code'] = 'RESPONSE_TOO_LARGE'
            return result
        result['body_sha256'] = hashlib.sha256(body).hexdigest()
        if response.status != check['expected_status']:
            result.update(status='failed', error_code='HTTP_STATUS_MISMATCH')
            return result
        try:
            document = decode_json(body)
        except (ValueError, UnicodeError, RecursionError):
            result.update(status='failed', error_code='INVALID_JSON', content_matches=False)
            return result
        expected = check['expected_json_subset']
        # 同时校验值和类型，避免 Python 将布尔值 True 与整数 1 视作相等。
        matches = isinstance(document, dict) and all(
            key in document and type(document[key]) is type(value) and document[key] == value
            for key, value in expected.items())
        if isinstance(document, dict):
            for key in expected:
                value = document.get(key)
                result['observed_fields'][key] = value if type(value) in (int, bool, type(None)) else str(value)[:300]
        result.update(status='passed' if matches else 'failed', content_matches=matches,
                      error_code=None if matches else 'JSON_CONTENT_MISMATCH')
    # 连接、DNS 或超时只表明本次未取得有效业务结果，保留 unknown。
    except socket.gaierror:
        result['error_code'] = 'DNS_ERROR'
    except (TimeoutError, socket.timeout):
        result['error_code'] = 'CHECK_TIMEOUT'
    except (OSError, http.client.HTTPException):
        result['error_code'] = 'CONNECTION_ERROR'
    finally:
        if connection is not None:
            connection.close()
        result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    return result
