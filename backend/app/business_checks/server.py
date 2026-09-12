# 集群内只读检查器：只从挂载配置查找登记目标，限制并发和单次检查时长。
# 调用方仅提供配置摘要、检查编号和请求标识，不能指定任意 URL 或命令。

"""A fixed-purpose in-cluster checker; no URLs, commands or credentials in requests."""
import json
import multiprocessing
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore

from backend.app.business_checks.protocol import (
    CHECK_ID, DIGEST, NONCE, decode_json, perform_check, result_base, validate_target,
)


def load_targets(path):
    raw = Path(path).read_bytes()
    if len(raw) > 131072:
        raise ValueError('checker config too large')
    targets = decode_json(raw)['targets']
    if not isinstance(targets, list) or not 1 <= len(targets) <= 10:
        raise ValueError('invalid target count')
    index = {}
    for target in targets:
        validate_target(target)
        key = (target['profile_digest'], target['check']['check_id'])
        if key in index:
            raise ValueError('duplicate registered check')
        index[key] = target
    return index


def _child(send, target, nonce):
    try:
        send.send(perform_check(target, nonce))
    finally:
        send.close()


def bounded_check(target, nonce):
    # A hard deadline also bounds DNS and slow-drip responses, not just socket idle time.
    started = time.monotonic()
    result = None
    ctx = multiprocessing.get_context('spawn')
    receive, send = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_child, args=(send, target, nonce), daemon=True)
    try:
        process.start()
    except Exception:
        receive.close()
        send.close()
        raise
    send.close()
    try:
        if receive.poll(target['check']['timeout_seconds'] + 0.5):
            try:
                result = receive.recv()
                return result
            except EOFError:
                result = result_base(target, nonce)
                result['error_code'] = 'CHECKER_WORKER_ERROR'
                return result
        result = result_base(target, nonce)
        result.update(error_code='CHECK_TIMEOUT')
        return result
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        receive.close()
        if isinstance(result, dict):
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000)


def make_server(config_path, host='0.0.0.0', port=8080, run_check=bounded_check):
    slots = BoundedSemaphore(4)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def reply(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            if self.path == '/livez':
                return self.reply(200, {'status': 'alive'})
            try:
                targets = load_targets(config_path)  # fresh file: stale digest requests fail closed
            except (OSError, ValueError, TypeError, KeyError):
                return self.reply(503, {'error': 'CHECKER_CONFIG_INVALID'})
            if self.path == '/readyz':
                return self.reply(200, {'status': 'ready'})
            parts = self.path.split('/')
            if (len(parts) != 5 or parts[1] != 'checks' or not DIGEST.fullmatch(parts[2])
                    or not CHECK_ID.fullmatch(parts[3]) or not NONCE.fullmatch(parts[4])):
                return self.reply(400, {'error': 'INVALID_CHECK_REQUEST'})
            target = targets.get((parts[2], parts[3]))
            if target is None:
                return self.reply(409, {'error': 'CHECK_NOT_REGISTERED_OR_CONFIG_STALE'})
            if not slots.acquire(blocking=False):
                return self.reply(429, {'error': 'CHECKER_BUSY'})
            try:
                result = run_check(target, parts[4])
                result['checker_pod'] = os.environ.get('POD_NAME', 'unknown')
                result['checker_pod_uid'] = os.environ.get('POD_UID', 'unknown')
                self.reply(200, result)
            except Exception:
                self.reply(503, {'error': 'CHECKER_INTERNAL_ERROR'})
            finally:
                slots.release()
        def do_POST(self):
            self.reply(405, {'error': 'METHOD_NOT_ALLOWED'})
        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST
    return ThreadingHTTPServer((host, port), Handler)


def main():
    with make_server('/etc/business-probe/targets.json') as server:
        server.serve_forever(poll_interval=0.1)


if __name__ == '__main__':
    main()
