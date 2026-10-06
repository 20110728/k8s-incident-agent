"""3A: real registered Deployment + Service, PostgreSQL, and production guards."""
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from backend.app.agent.approval import build_approval_request, create_approval_record
from backend.app.agent.schemas import ApprovalDecision, RemediationPlan
from backend.app.agent.target_identity import bind_target
from backend.app.persistence.operations import approval_binding
from backend.app.runtime.operations import LedgerExecutor
from backend.app.service_profiles.models import ServiceProfile
from backend.app.service_profiles.registry import make_snapshot, ProfileUnavailable
from backend.app.tools.client import create_clients, REQUEST_TIMEOUT
from backend.app.tools.workload_tools import get_deployment_config
from backend.tests.runtime.test_operations_postgres import state_with_uid, decision_for, waiting
from backend.tests.runtime.test_worker_postgres import storage, wait_until

pytestmark = pytest.mark.skipif(os.environ.get("STAGE3A_KIND") != "1", reason="requires stage 3A real kind acceptance")


@pytest.mark.parametrize("change", ["unchanged", "service_recreated", "deployment_recreated", "release_changed", "profile_changed"])
def test_3A_approved_identity_guards_real_write(storage, tmp_path, monkeypatch, change):
    connect, repo, _ = storage
    clients = create_clients(disable_retries=True)
    name = "stage3a-" + uuid4().hex[:16]
    namespace = "agent-demo"
    owned = {}
    calls = []
    audit = Path(os.environ["INCIDENT_AGENT_TEST_AUDIT_DIR"]) / (name + "-" + change)
    audit.mkdir(parents=True)
    def save(key, value):
        (audit / (key + ".json")).write_text(json.dumps(value, default=str, indent=2), encoding="utf-8")
    def create_service():
        obj = clients.core.create_namespaced_service(namespace, client.V1Service(
            metadata=client.V1ObjectMeta(name=name, labels={"incident-agent-acceptance": "3a"}),
            spec=client.V1ServiceSpec(selector={"app": "wrong-service"}, ports=[client.V1ServicePort(port=80)])),
            _request_timeout=REQUEST_TIMEOUT)
        owned["Service"] = obj.metadata.uid
        return obj
    def create_deployment(profile):
        # Zero replicas: inspect real release configuration without downloading images.
        obj = clients.apps.create_namespaced_deployment(namespace, client.V1Deployment(
            metadata=client.V1ObjectMeta(name=name, labels={"incident-agent-acceptance": "3a"}),
            spec=client.V1DeploymentSpec(replicas=0, selector=client.V1LabelSelector(match_labels={"stage3a": name}),
                template=client.V1PodTemplateSpec(metadata=client.V1ObjectMeta(labels={
                    **profile.expected_selector, "stage3a": name, profile.application.version_label: profile.application.version}),
                    spec=client.V1PodSpec(containers=[client.V1Container(name=profile.container_name,
                        image=profile.application.images[profile.container_name],
                        readiness_probe=client.V1Probe(http_get=client.V1HTTPGetAction(path=profile.readiness_probe.path,
                                                                                  port=profile.readiness_probe.port)))])))),
            _request_timeout=REQUEST_TIMEOUT)
        owned["Deployment"] = obj.metadata.uid
        return obj
    def remove(kind):
        method = clients.core.delete_namespaced_service if kind == "Service" else clients.apps.delete_namespaced_deployment
        method(name, namespace, body=client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=owned[kind])),
               _request_timeout=REQUEST_TIMEOUT)
        reader = clients.core.read_namespaced_service if kind == "Service" else clients.apps.read_namespaced_deployment
        def gone():
            try:
                reader(name, namespace, _request_timeout=REQUEST_TIMEOUT)
                return False
            except ApiException as exc:
                if exc.status != 404:
                    raise
                return True
        wait_until(gone, timeout=30)
        owned.pop(kind)
    try:
        state = state_with_uid()
        raw_profile = deepcopy(state["service_profile"]["profile"])
        raw_profile.update(service_name=name, deployment_name=name)
        profile = ServiceProfile.model_validate(raw_profile)
        path = tmp_path / "registered.json"
        path.write_text(profile.model_dump_json(), encoding="utf-8")
        monkeypatch.setenv("INCIDENT_AGENT_SERVICE_PROFILE_DIR", str(tmp_path))
        service = create_service()
        create_deployment(profile)
        deployment = get_deployment_config(clients, namespace, name).model_dump(mode="json")
        state["request"]["service_name"] = name
        state["remediation_plan"]["parameters"]["resource_name"] = name
        state["evidence"][0]["resource_name"] = name
        state["evidence"][0]["data"].update(name=name, uid=service.metadata.uid)
        for evidence in state["evidence"]:
            if evidence["resource_type"] == "Deployment":
                evidence.update(resource_name=name, data=deployment)
            if evidence["resource_type"] == "OwnerChain":
                evidence["data"]["owner_chain"].update(deployment_name=name, deployment_uid=deployment["uid"])
        state["service_profile"] = make_snapshot(profile, deployment)
        state["remediation_plan"] = bind_target(RemediationPlan.model_validate(state["remediation_plan"]), state).model_dump(mode="json")
        request = build_approval_request(state)
        decision = ApprovalDecision(**{**decision_for(state), "approval_id": request.approval_id})
        state["approval_request"] = request.model_dump(mode="json")
        state["approval_record"] = create_approval_record(request, decision).model_dump(mode="json")
        row = waiting(repo, state)
        repo.queue_approval(row["run_id"], decision.model_dump(mode="json"), approval_binding(state))
        saved_approval = deepcopy(repo.latest(row["incident_id"])["approval_payload"])
        save("approved", {"request": state["approval_request"], "record": state["approval_record"],
                          "service_uid": service.metadata.uid, "deployment": deployment})
        if change == "service_recreated":
            remove("Service")
            assert create_service().metadata.uid != service.metadata.uid
        elif change == "deployment_recreated":
            remove("Deployment")
            assert create_deployment(profile).metadata.uid != deployment["uid"]
        elif change == "release_changed":
            clients.apps.patch_namespaced_deployment(name, namespace, {"spec": {"template": {"metadata": {
                "labels": {profile.application.version_label: "changed-after-approval"}}}}}, _request_timeout=REQUEST_TIMEOUT)
        elif change == "profile_changed":
            raw_profile["application"]["version"] = "changed-after-approval"
            path.write_text(json.dumps(raw_profile), encoding="utf-8")
        lease = repo.claim("stage3a", 60)
        class Transport:
            def read_namespaced_service(self, **kwargs):
                return clients.core.read_namespaced_service(**kwargs)
            def patch_namespaced_service(self, **kwargs):
                calls.append(deepcopy(kwargs["body"]))
                return clients.core.patch_namespaced_service(**kwargs)
        executor = LedgerExecutor(NS(core=Transport(), apps=clients.apps), repo, lease)
        if change in {"service_recreated", "deployment_recreated", "release_changed", "profile_changed"}:
            with pytest.raises(ProfileUnavailable):
                executor.execute(state)
        else:
            result = executor.execute(state)
            assert result.status == ("succeeded" if change == "unchanged" else "conflict")
        assert len(calls) == int(change == "unchanged")
        current = clients.core.read_namespaced_service(name, namespace, _request_timeout=REQUEST_TIMEOUT)
        assert current.spec.selector == ({"app": "order-service"} if change == "unchanged" else {"app": "wrong-service"})
        assert repo.latest(row["incident_id"])["approval_payload"] == saved_approval
        operation = repo.operation(row["run_id"])
        if operation:
            assert operation["plan_revision"] == request.plan_revision
        save("after", {"service_uid": current.metadata.uid, "selector": current.spec.selector,
                       "deployment": get_deployment_config(clients, namespace, name).model_dump(mode="json"),
                       "operation": operation, "agent_patch_requests": calls})
        with connect() as connection:
            save("database", connection.execute("SELECT current_database() AS name").fetchone())
    finally:
        save("patch-requests", calls)
        for kind in list(owned):
            try:
                remove(kind)
            except ApiException as exc:
                if exc.status != 404:
                    raise
