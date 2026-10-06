"""Real ServiceAccount tokens and server responses; no impersonated mutations."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

import pytest

from backend.app.agent.approval import build_approval_request, create_approval_record
from backend.app.agent.schemas import ApprovalDecision, RemediationPlan
from backend.app.agent.target_identity import bind_target
from backend.app.persistence.operations import approval_binding
from backend.app.runtime.operations import LedgerExecutor
from backend.app.service_profiles.models import ServiceProfile
from backend.app.service_profiles.registry import make_snapshot, validate_profile_action, ProfileUnavailable
from backend.app.tools.workload_tools import get_deployment_config
from backend.tests.rbac.lab import Lab, NS
from backend.tests.runtime.test_operations_postgres import state_with_uid, decision_for, waiting
from backend.tests.runtime.test_worker_postgres import storage

pytestmark = pytest.mark.skipif(os.environ.get("STAGE3B_RBAC") != "1", reason="requires 3B real credential acceptance")


@pytest.mark.parametrize("mode", ["reader", "remediator"])
def test_3B_real_identity_matrix_and_approved_repair(storage, tmp_path, monkeypatch, mode):
    connect, repo, _ = storage
    with TemporaryDirectory(prefix="stage3b-credentials-") as private:
        directory = Path(os.environ["INCIDENT_AGENT_TEST_AUDIT_DIR"]) / mode
        directory.mkdir()
        lab = Lab(directory, Path(private))
        try:
            name = lab.name
            other = name + "-other"
            lab.create("Namespace", other, namespace=None)
            state = state_with_uid()
            raw_profile = deepcopy(state["service_profile"]["profile"])
            raw_profile.update(service_name=name, deployment_name=name)
            profile = ServiceProfile.model_validate(raw_profile)
            (tmp_path / "profile.json").write_text(profile.model_dump_json(), encoding="utf-8")
            monkeypatch.setenv("INCIDENT_AGENT_SERVICE_PROFILE_DIR", str(tmp_path))
            service_spec = {"selector": {"app": "wrong-service"}, "ports": [{"port": 80}]}
            service = lab.create("Service", name, spec=service_spec)
            lab.create("Service", other, spec=service_spec)
            lab.create("Service", name, namespace=other, spec=service_spec)
            deployment_spec = {"replicas": 0, "selector": {"matchLabels": {"stage3b": name}},
                "template": {"metadata": {"labels": {**profile.expected_selector, "stage3b": name,
                    profile.application.version_label: profile.application.version}},
                    "spec": {"automountServiceAccountToken": False, "containers": [{"name": profile.container_name,
                        "image": profile.application.images[profile.container_name],
                        "readinessProbe": {"httpGet": {"path": profile.readiness_probe.path, "port": profile.readiness_probe.port}}}]}}}
            lab.create("Deployment", name, spec=deployment_spec)
            lab.create("Deployment", other, spec=deployment_spec)
            lab.create("Secret", name, type="Opaque", stringData={"acceptance": "non-secret-test-value"})
            api, kubeconfig, subject = lab.identity(mode)
            restricted = lab.clients(api)
            def request(label, method, path, expected=200, **kwargs):
                return lab.request(api, subject, label, method, path, expected, **kwargs)
            base = f"/api/v1/namespaces/{NS}"
            apps = f"/apis/apps/v1/namespaces/{NS}"
            for label, path in [
                ("pods.list", base + "/pods"), ("services.get", base + "/services/" + name),
                ("other_service.read_allowed", base + "/services/" + other),
                ("events.list", base + "/events"), ("deployments.get", apps + "/deployments/" + name),
                ("replicasets.list", apps + "/replicasets"),
                ("endpointslices.list", f"/apis/discovery.k8s.io/v1/namespaces/{NS}/endpointslices")]:
                request(label, "GET", path)
            pods = lab.core.list_namespaced_pod(NS, label_selector="app=incident-agent-business-probe", _request_timeout=(3, 10)).items
            pods = [pod for pod in pods if pod.metadata.deletion_timestamp is None
                    and any(condition.type == "Ready" and condition.status == "True" for condition in (pod.status.conditions or []))]
            assert pods, "a ready business checker is required; deploy infra/business-probe before 3B"
            pod = pods[0].metadata.name
            request("pods.get", "GET", base + "/pods/" + pod)
            request("pods.log", "GET", base + "/pods/" + pod + "/log", query=[("tailLines", 1)])
            node = lab.core.list_node(_request_timeout=(3, 10)).items[0].metadata.name
            request("nodes.get", "GET", "/api/v1/nodes/" + node)
            checker = request("checker.proxy", "GET", base + "/services/incident-agent-business-probe:80/proxy/readyz")
            assert checker.get("status") == "ready", "checker proxy did not return a ready response"
            for label, path in [("secrets.get", base + "/secrets/" + name), ("secrets.list", base + "/secrets"),
                ("other_namespace.read", f"/api/v1/namespaces/{other}/services/{name}"),
                ("nodes.list", "/api/v1/nodes"),
                ("other_service.proxy", base + "/services/" + other + "/proxy/readyz")]:
                request(label, "GET", path, 403)
            # Dry-run denied writes cannot mutate even if a policy is accidentally too broad.
            patch = {"metadata": {"annotations": {"stage3b": "permission-probe"}}}
            for label, path, expected in [
                ("registered_service.patch", base + "/services/" + name, 200 if mode == "remediator" else 403),
                ("registered_deployment.patch", apps + "/deployments/" + name, 200 if mode == "remediator" else 403),
                ("other_service.patch", base + "/services/" + other, 403),
                ("other_deployment.patch", apps + "/deployments/" + other, 403),
                ("other_namespace.patch", f"/api/v1/namespaces/{other}/services/{name}", 403)]:
                request(label, "PATCH", path, expected, body=patch, query=[("dryRun", "All")], content_type="application/merge-patch+json")
            for label, path in [("pods.delete", base + "/pods/" + pod),
                                 ("services.delete", base + "/services/" + name),
                                 ("deployments.delete", apps + "/deployments/" + name)]:
                request(label, "DELETE", path, 403, body={"apiVersion": "v1", "kind": "DeleteOptions", "dryRun": ["All"]})
            # A nonexistent temporary Pod prevents execution if unexpected privilege exists.
            request("pods.exec", "POST", base + "/pods/" + name + "-absent/exec", 403,
                    query=[("command", "true"), ("stdout", "true")])
            request("pods.create", "POST", base + "/pods", 403, query=[("dryRun", "All")],
                    body={"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name},
                          "spec": {"containers": [{"name": "test", "image": "unused:acceptance"}]}})
            request("rolebindings.create", "POST", f"/apis/rbac.authorization.k8s.io/v1/namespaces/{NS}/rolebindings", 403,
                    query=[("dryRun", "All")], body={"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
                        "metadata": {"name": name}, "subjects": [],
                        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name}})
            env = dict(os.environ, RBAC_KUBECONFIG=str(kubeconfig), RBAC_SUBJECT="", RBAC_TARGET=name, RBAC_OTHER_TARGET=other)
            auxiliary = subprocess.run(["bash", "scripts/check_rbac.sh", mode], env=env, capture_output=True, text=True, timeout=90)
            (directory / "can-i.txt").write_text(auxiliary.stdout + auxiliary.stderr, encoding="utf-8")
            assert auxiliary.returncode == 0, "auxiliary matrix failed; see can-i.txt"
            if mode == "remediator":
                deployment = get_deployment_config(restricted, NS, name).model_dump(mode="json")
                state["request"]["service_name"] = name
                state["remediation_plan"]["parameters"]["resource_name"] = name
                state["evidence"][0]["resource_name"] = name
                state["evidence"][0]["data"].update(name=name, uid=service["metadata"]["uid"])
                for item in state["evidence"]:
                    if item["resource_type"] == "Deployment":
                        item.update(resource_name=name, data=deployment)
                    if item["resource_type"] == "OwnerChain":
                        item["data"]["owner_chain"].update(deployment_name=name, deployment_uid=deployment["uid"])
                state["service_profile"] = make_snapshot(profile, deployment)
                plan = bind_target(RemediationPlan.model_validate(state["remediation_plan"]), state)
                state["remediation_plan"] = plan.model_dump(mode="json")
                approval = build_approval_request(state)
                decision = ApprovalDecision(**{**decision_for(state), "approval_id": approval.approval_id})
                state["approval_request"] = approval.model_dump(mode="json")
                state["approval_record"] = create_approval_record(approval, decision).model_dump(mode="json")
                row = waiting(repo, state)
                repo.queue_approval(row["run_id"], decision.model_dump(mode="json"), approval_binding(state))
                lease = repo.claim("stage3b-remediator", 60)
                result = LedgerExecutor(restricted, repo, lease).execute(state)
                assert result.status == "succeeded"
                after = restricted.core.read_namespaced_service(name, NS, _request_timeout=(3, 10))
                assert after.spec.selector == profile.expected_selector and after.metadata.uid == service["metadata"]["uid"]
                operation = repo.operation(row["run_id"])
                assert operation["state"] == "succeeded"
                lab.save("approved-repair", {"subject": subject, "approval": state["approval_record"],
                    "before": service, "result": result.model_dump(mode="json"), "operation": operation})
                invalid = plan.model_copy(deep=True)
                invalid.parameters.proposed_selector[0].value = "not-registered"
                with pytest.raises(ProfileUnavailable):
                    validate_profile_action(profile, invalid)
                lab.save("field-boundary", {"rbac": "metadata annotation dry-run allowed on registered objects",
                    "application": "unregistered selector rejected by validate_profile_action",
                    "note": "RBAC is not field-level authorization; approver is a single-operator label, not authenticated identity"})
            with connect() as connection:
                lab.save("database", connection.execute("SELECT current_database() AS name").fetchone())
        finally:
            lab.close()
