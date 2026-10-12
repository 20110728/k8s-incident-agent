"""Opt-in L3: real Service PATCH with synthetic approved release evidence.

Only uniquely named stage2b-* Services are created/removed in agent-demo.
This does not exercise LLM diagnosis, real release-profile collection or RBAC.
"""
from types import SimpleNamespace as NS
from uuid import uuid4
import os

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from backend.app.agent.approval import build_approval_request
from backend.app.persistence.operations import approval_binding
from backend.app.runtime import operations as module
from backend.app.runtime.operations import LedgerExecutor, OutcomeUnknown
from backend.app.service_profiles.models import ServiceProfile
from backend.app.service_profiles.registry import profile_digest
from backend.app.tools.client import create_clients, REQUEST_TIMEOUT
from backend.tests.runtime.test_operations_postgres import state_with_uid, decision_for, waiting
from backend.tests.runtime.test_worker_postgres import storage, expire

pytestmark = pytest.mark.skipif(os.environ.get("STAGE2B_KIND") != "1", reason="opt in with STAGE2B_KIND=1")


@pytest.mark.parametrize("mode", ["timeout_applied", "third_value", "recreated", "lost_response_commit"])
def test_real_patch_with_unknown_outcome_never_reissues(storage, monkeypatch, mode):
    connect, repo, _ = storage
    clients = create_clients(disable_retries=True)
    name = "stage2b-" + uuid4().hex[:16]
    created = clients.core.create_namespaced_service("agent-demo", client.V1Service(
        metadata=client.V1ObjectMeta(name=name, labels={"incident-agent-acceptance": "2b"}),
        spec=client.V1ServiceSpec(selector={"app": "wrong-service"},
                                  ports=[client.V1ServicePort(port=80, target_port=80)])), _request_timeout=REQUEST_TIMEOUT)
    uid = created.metadata.uid
    cleanup_uid = uid
    calls = []
    try:
        state = state_with_uid()
        state["request"]["service_name"] = name
        state["remediation_plan"]["parameters"]["resource_name"] = name
        state["evidence"][0]["resource_name"] = name
        state["evidence"][0]["data"].update(name=name, uid=uid)
        state["service_profile"]["profile"]["service_name"] = name
        profile = ServiceProfile.model_validate(state["service_profile"]["profile"])
        state["service_profile"]["digest"] = profile_digest(profile)
        state["approval_request"] = build_approval_request(state).model_dump(mode="json")
        state["approval_record"]["approval_id"] = state["approval_request"]["approval_id"]
        row = waiting(repo, state)
        repo.queue_approval(row["run_id"], decision_for(state), approval_binding(state))
        lease = repo.claim("kind-writer", 60)
        # Test profile is synthetic, but the operation journal, HTTP request,
        # resourceVersion preconditions and all observations are real.
        monkeypatch.setattr(module, "revalidate_live_profile", lambda *args: {})
        class Transport:
            def read_namespaced_service(self, **kwargs):
                return clients.core.read_namespaced_service(**kwargs)
            def patch_namespaced_service(self, **kwargs):
                calls.append(kwargs["body"])
                result = clients.core.patch_namespaced_service(**kwargs)
                if mode != "lost_response_commit":
                    raise TimeoutError("simulated lost response after real API applied patch")
                return result
        executor = LedgerExecutor(NS(core=Transport(), apps=clients.apps), repo, lease)
        if mode == "lost_response_commit":
            record = repo.record
            monkeypatch.setattr(repo, "record", lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError()))
            with pytest.raises(ConnectionError):
                executor.execute(state)
            monkeypatch.setattr(repo, "record", record)
        else:
            with pytest.raises(OutcomeUnknown):
                executor.execute(state)
        actual = clients.core.read_namespaced_service(name, "agent-demo", _request_timeout=REQUEST_TIMEOUT)
        assert actual.spec.selector == {"app": "order-service"}
        assert actual.metadata.resource_version != created.metadata.resource_version
        if mode == "third_value":
            clients.core.patch_namespaced_service(name, "agent-demo", [
                {"op": "test", "path": "/metadata/uid", "value": uid},
                {"op": "test", "path": "/metadata/resourceVersion", "value": actual.metadata.resource_version},
                {"op": "replace", "path": "/spec/selector", "value": {"app": "third-value"}},
            ], _request_timeout=REQUEST_TIMEOUT)
        if mode == "recreated":
            clients.core.delete_namespaced_service(name, "agent-demo",
                body=client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=uid)), _request_timeout=REQUEST_TIMEOUT)
            recreated = clients.core.create_namespaced_service("agent-demo", client.V1Service(
                metadata=client.V1ObjectMeta(name=name, labels={"incident-agent-acceptance": "2b"}),
                spec=client.V1ServiceSpec(selector={"app": "order-service"},
                                          ports=[client.V1ServicePort(port=80, target_port=80)])), _request_timeout=REQUEST_TIMEOUT)
            cleanup_uid = recreated.metadata.uid
            assert cleanup_uid != uid
        expire(connect, lease["run_id"])
        recovered = repo.claim("kind-recovery", 60)
        reconciler = LedgerExecutor(clients, repo, recovered)
        assert reconciler.reconcile(repo.operation(row["run_id"])) is False
        with pytest.raises(OutcomeUnknown):
            reconciler.execute(state)
        operation = repo.operation(row["run_id"])
        assert operation["state"] == "manual_required"
        assert operation["observed_snapshot"]["uid"] == cleanup_uid
        assert len(calls) == 1
    finally:
        try:
            clients.core.delete_namespaced_service(name, "agent-demo",
                body=client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=cleanup_uid)), _request_timeout=REQUEST_TIMEOUT)
        except ApiException as error:
            if error.status != 404:
                raise
