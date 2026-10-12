"""Exercise installed Kubernetes SDK serialization without a network request."""
import json
from types import SimpleNamespace as NS

import pytest
from kubernetes import client

from backend.app.runtime.operations import build_patch
from backend.app.tools import client as client_module
from backend.tests.agent.test_executor_and_node import readiness_plan, selector_plan


@pytest.mark.parametrize("kind", ["Service", "Deployment"])
def test_sdk_sends_list_as_json_patch_and_deserializes_actual_metadata(monkeypatch, kind):
    api = client.ApiClient(client.Configuration())
    sent = []
    def request(method, url, **kwargs):
        sent.append((method, kwargs))
        body = {"apiVersion": "v1" if kind == "Service" else "apps/v1", "kind": kind,
                "metadata": {"uid": "actual-uid", "resourceVersion": "203", "generation": 7}}
        return NS(data=json.dumps(body).encode(), status=200, reason="OK", getheaders=lambda: {},
                  getheader=lambda name, default=None: "application/json" if name.lower() == "content-type" else default)
    monkeypatch.setattr(api.rest_client, "request", request)
    method = (client.CoreV1Api(api).patch_namespaced_service if kind == "Service"
              else client.AppsV1Api(api).patch_namespaced_deployment)
    patch = [{"op": "test", "path": "/metadata/uid", "value": "actual-uid"}]
    result = method(name="test", namespace="test", body=patch, _request_timeout=(3, 10))
    assert len(sent) == 1 and sent[0][0] == "PATCH"
    assert sent[0][1]["headers"]["Content-Type"] == "application/json-patch+json"
    assert sent[0][1]["body"] == patch
    assert (result.metadata.uid, result.metadata.resource_version, result.metadata.generation) == ("actual-uid", "203", 7)


def test_write_client_has_no_hidden_transport_retries(monkeypatch):
    monkeypatch.setattr(client_module.config, "load_incluster_config", lambda: None)
    clients = client_module.create_clients(disable_retries=True)
    assert clients.core.api_client is clients.apps.api_client
    assert clients.core.api_client.configuration.retries == 0


def test_readiness_patch_checks_identity_version_container_and_old_values():
    resource = NS(spec=NS(template=NS(spec=NS(containers=[NS(name="sidecar"), NS(name="order-service")]))))
    before = {"uid": "deployment-uid", "resource_version": "12", "configuration": {
        "container_name": "order-service", "readiness_probe": {"path": "/wrong-health", "port": "http"}}}
    patch = build_patch(readiness_plan(), resource, before)
    assert patch[:2] == [{"op": "test", "path": "/metadata/uid", "value": "deployment-uid"},
                         {"op": "test", "path": "/metadata/resourceVersion", "value": "12"}]
    assert patch[2] == {"op": "test", "path": "/spec/template/spec/containers/1/name", "value": "order-service"}
    writes = [item for item in patch if item["op"] != "test"]
    assert writes == [
        {"op": "replace", "path": "/spec/template/spec/containers/1/readinessProbe/httpGet/path", "value": "/healthz"},
        {"op": "replace", "path": "/spec/template/spec/containers/1/readinessProbe/httpGet/port", "value": "http"},
    ]


def test_selector_patch_only_replaces_selector():
    before = {"uid": "service-uid", "resource_version": "12", "configuration": {"selector": {"app": "wrong-service"}}}
    patch = build_patch(selector_plan(), None, before)
    assert [item for item in patch if item["op"] != "test"] == [
        {"op": "replace", "path": "/spec/selector", "value": {"app": "order-service"}}]
