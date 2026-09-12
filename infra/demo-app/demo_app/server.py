# 演示应用：订单 HTTP 接口与独立模拟下游共用实现，通过角色启动为不同服务。
# 存活与业务就绪分离；故障模式可产生依赖不可用、HTTP 500 和内容错误。

"""Order API and independent mock dependency. Python standard library only."""
import argparse
import json
import os
from dataclasses import dataclass
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from demo_app import VERSION

ORDER_PATH = "/api/orders/demo-001"
DEPENDENCY_PATH = "/api/availability/demo-001"
MAX_DEPENDENCY_BYTES = 16384
FAULT_MODES = {"normal", "api500", "wrong_content"}


@dataclass(frozen=True)
class Settings:
    role: str
    fault_mode: str = "normal"
    dependency_url: str = "http://order-dependency.agent-demo.svc.cluster.local"
    dependency_timeout: float = 0.8

    def __post_init__(self):
        if self.role not in {"order", "dependency"}:
            raise ValueError("role must be order or dependency")
        if self.fault_mode not in FAULT_MODES:
            raise ValueError("unknown ORDER_FAULT_MODE")
        if not 0 < self.dependency_timeout <= 1:
            raise ValueError("dependency_timeout must be in (0, 1] seconds")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def dependency_available(settings: Settings) -> bool:
    # No retries, no redirect following, bounded body. Ignore ambient HTTP proxies.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    request = Request(settings.dependency_url.rstrip("/") + DEPENDENCY_PATH,
                      headers={"Accept": "application/json"}, method="GET")
    try:
        with opener.open(request, timeout=settings.dependency_timeout) as response:
            body = response.read(MAX_DEPENDENCY_BYTES + 1)
            if response.status != 200 or len(body) > MAX_DEPENDENCY_BYTES:
                return False
            data = json.loads(body)
            return (isinstance(data, dict) and data.get("order_id") == "demo-001"
                    and data.get("available") is True)
    except HTTPError as error:
        error.close()
        return False
    except (URLError, TimeoutError, OSError, ValueError, HTTPException):
        return False


def response_for(settings: Settings, path: str) -> tuple[int, dict]:
    service = "order-service" if settings.role == "order" else "order-dependency"
    identity = {"service": service, "version": VERSION}
    if path == "/livez":
        return 200, {**identity, "status": "alive"}
    if settings.role == "dependency":
        if path == "/readyz":
            return 200, {**identity, "status": "ready"}
        if path == DEPENDENCY_PATH:
            return 200, {**identity, "order_id": "demo-001", "available": True}
        return 404, {**identity, "error": "not_found"}
    if path not in {"/readyz", ORDER_PATH}:
        return 404, {**identity, "error": "not_found"}
    if not dependency_available(settings):
        return 503, {**identity, "error": "dependency_unavailable"}
    if path == "/readyz":
        return 200, {**identity, "status": "ready"}
    if settings.fault_mode == "api500":
        return 500, {**identity, "error": "simulated_order_processing_error"}
    if settings.fault_mode == "wrong_content":
        return 200, {**identity, "order_id": "wrong-order", "status": "cancelled",
                     "dependency_status": "available"}
    return 200, {**identity, "order_id": "demo-001", "status": "confirmed",
                 "dependency_status": "available"}


def make_server(settings: Settings, host: str = "0.0.0.0", port: int = 8080):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            # No raw terminal escape sequences from request data.
            print(json.dumps({"service": settings.role, "message": (fmt % args)[:512]}), flush=True)

        def send_json(self, status: int, data: dict):
            body = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(body)

        def do_GET(self):
            status, data = response_for(settings, self.path)
            self.send_json(status, data)

        def do_POST(self):
            self.send_json(405, {"error": "method_not_allowed"})

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["order", "dependency"])
    args = parser.parse_args()
    settings = Settings(role=args.role, fault_mode=os.getenv("ORDER_FAULT_MODE", "normal"),
                        dependency_url=os.getenv("DEPENDENCY_URL", Settings.dependency_url))
    with make_server(settings) as server:
        print(json.dumps({"event": "started", "role": settings.role,
                          "version": VERSION, "fault_mode": settings.fault_mode}), flush=True)
        try:
            server.serve_forever(poll_interval=0.1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
