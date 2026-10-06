"""Two allowlisted JSON PATCH operations, durably recorded before dispatch."""
from copy import deepcopy
from datetime import datetime, UTC
from types import SimpleNamespace

from kubernetes.client.exceptions import ApiException

from backend.app.agent.execution_policy import validate_execution_authorization
from backend.app.agent.schemas import ActionExecutionResult, ResourceSnapshot
from backend.app.persistence.operations import approval_binding, json_value
from backend.app.persistence.leases import LeaseLost
from backend.app.persistence.runs import request_digest
from backend.app.service_profiles.registry import revalidate_live_profile
from backend.app.tools.client import REQUEST_TIMEOUT
from backend.app.tools.remediation_tools import _service_snapshot, _deployment_snapshot
from backend.app.runtime.failpoints import hit
from backend.app.runtime.telemetry import report


class OutcomeUnknown(RuntimeError):
    pass


def read_target(clients, parameters):
    if parameters.resource_kind == "Service":
        resource = clients.core.read_namespaced_service(name=parameters.resource_name,
                    namespace=parameters.namespace, _request_timeout=REQUEST_TIMEOUT)
        snapshot = _service_snapshot(namespace=parameters.namespace, service_name=parameters.resource_name, service=resource)
    else:
        resource = clients.apps.read_namespaced_deployment(name=parameters.resource_name,
                    namespace=parameters.namespace, _request_timeout=REQUEST_TIMEOUT)
        snapshot = _deployment_snapshot(namespace=parameters.namespace, deployment_name=parameters.resource_name,
                                       deployment=resource, container_name=parameters.container_name)
    snapshot.uid = getattr(resource.metadata, "uid", None)
    snapshot.generation = getattr(resource.metadata, "generation", None)
    if not snapshot.uid:
        raise ValueError("target UID is missing")
    return resource, snapshot.model_dump(mode="json")


def desired_configuration(plan):
    p = plan.parameters
    if plan.action == "patch_service_selector":
        return {"selector": {item.key: item.value for item in p.proposed_selector}}
    return {"container_name": p.container_name,
            "readiness_probe": {"path": p.proposed_probe_path, "port": p.proposed_probe_port}}


def prior_configuration(plan):
    p = plan.parameters
    if plan.action == "patch_service_selector":
        return {"selector": {item.key: item.value for item in p.current_selector}}
    return {"container_name": p.container_name,
            "readiness_probe": {"path": p.current_probe_path, "port": p.current_probe_port}}


def approved_uid(state, parameters):
    if parameters.resource_kind == "Deployment":
        return (state.get("service_profile") or {}).get("deployment_uid")
    matches = [item.get("data", {}).get("uid") for item in state.get("evidence", [])
               if item.get("resource_type") == "Service" and item.get("resource_name") == parameters.resource_name
               and item.get("data", {}).get("namespace") == parameters.namespace]
    return matches[0] if len(matches) == 1 else None


def build_patch(plan, resource, before):
    patch = [{"op": "test", "path": "/metadata/uid", "value": before["uid"]},
             {"op": "test", "path": "/metadata/resourceVersion", "value": before["resource_version"]}]
    if plan.action == "patch_service_selector":
        patch += [{"op": "test", "path": "/spec/selector", "value": before["configuration"]["selector"]},
                  {"op": "replace", "path": "/spec/selector", "value": desired_configuration(plan)["selector"]}]
    else:
        containers = resource.spec.template.spec.containers
        index = next(i for i, item in enumerate(containers) if item.name == plan.parameters.container_name)
        base = f"/spec/template/spec/containers/{index}"
        patch.append({"op": "test", "path": base + "/name", "value": plan.parameters.container_name})
        for key in ("path", "port"):
            path = base + "/readinessProbe/httpGet/" + key
            patch.extend([{"op": "test", "path": path, "value": before["configuration"]["readiness_probe"][key]},
                          {"op": "replace", "path": path, "value": desired_configuration(plan)["readiness_probe"][key]}])
    return patch


class LedgerExecutor:
    def __init__(self, clients, repository, lease, lost=None):
        self.clients, self.repository, self.lease = clients, repository, lease
        self.lost = lost

    def _assert_owned(self):
        if self.lost is not None and self.lost.is_set():
            raise LeaseLost("heartbeat failed before dispatch")
        self.repository.assert_owned(self.lease)

    def execute(self, state):
        self._assert_owned()
        state = json_value(state)
        auth = validate_execution_authorization(state)
        saved = self.lease.get("approval_payload")
        if not saved or not saved["decision"]["approved"] or saved["binding"] != approval_binding(state):
            raise ValueError("durable approval binding does not match")
        if saved["decision"]["approval_id"] != auth.approval_id:
            raise ValueError("durable approval ID does not match")
        if any(saved["decision"].get(key) != getattr(auth.approval_record, key)
               for key in ("approved", "approver", "comment")):
            raise ValueError("durable approval record does not match")
        revision = request_digest(auth.plan.model_dump(mode="json"))
        operation = self.repository.operation(self.lease["run_id"])
        if operation and (operation["approval_id"] != auth.approval_id or operation["plan_revision"] != revision):
            raise ValueError("operation authorization changed")
        if operation and operation["state"] in {"succeeded", "reconciled", "rejected"} and operation["result"] is not None:
            return ActionExecutionResult.model_validate(operation["result"])
        if operation and operation["state"] != "prepared":
            raise OutcomeUnknown("operation requires read-only reconciliation")

        live = revalidate_live_profile(state, self.clients, auth.plan)
        resource, before = read_target(self.clients, auth.plan.parameters)
        target = {**before, "configuration": desired_configuration(auth.plan)}
        patch = build_patch(auth.plan, resource, before)
        operation = self.repository.prepare(self.lease, approval_id=auth.approval_id, plan_revision=revision,
            plan=auth.plan.model_dump(mode="json"), action=auth.plan.action, before=before, target=target, patch=patch)
        report("operation_prepared", self.lease, operation_id=operation["operation_id"], node="execute_remediation",
               input_value=operation["request_patch"], output_value=operation["before_snapshot"])
        hit("after_operation_prepare", self.lease, operation["operation_id"])
        # A prepared operation can be resumed only against its exact original target.
        if (before != operation["before_snapshot"] or before["uid"] != approved_uid(state, auth.plan.parameters) or
                (auth.plan.parameters.resource_kind == "Deployment" and before["resource_version"] != live["resource_version"]) or
                before["configuration"] != prior_configuration(auth.plan)):
            return self._result(auth, operation, "conflict", code="OPERATION_PRECONDITION_FAILED")
        self.repository.dispatch(self.lease, operation["operation_id"])
        hit("before_patch", self.lease, operation["operation_id"])
        self._assert_owned()
        report("patch_dispatch", self.lease, operation_id=operation["operation_id"], node="execute_remediation")
        try:
            method = (self.clients.core.patch_namespaced_service if auth.plan.parameters.resource_kind == "Service"
                      else self.clients.apps.patch_namespaced_deployment)
            response = method(name=auth.plan.parameters.resource_name, namespace=auth.plan.parameters.namespace,
                              body=deepcopy(operation["request_patch"]),
                              _request_timeout=REQUEST_TIMEOUT)
        except ApiException as error:
            if error.status in {400, 401, 403, 404, 409, 422}:
                return self._result(auth, operation, "conflict", code="KUBERNETES_REJECTED")
            self.repository.record(self.lease, "outcome_unknown", code="WRITE_OUTCOME_UNKNOWN")
            raise OutcomeUnknown("patch response is unknown") from None
        except Exception:
            self.repository.record(self.lease, "outcome_unknown", code="WRITE_OUTCOME_UNKNOWN")
            raise OutcomeUnknown("patch response is unknown") from None
        report("patch_response", self.lease, operation_id=operation["operation_id"], node="execute_remediation",
               output_value=response.to_dict() if hasattr(response, "to_dict") else None)
        hit("after_patch_response", self.lease, operation["operation_id"])
        # Record the actual API response, never a post-hoc GET or inferred version.
        try:
            if auth.plan.parameters.resource_kind == "Service":
                snapshot = _service_snapshot(namespace=auth.plan.parameters.namespace,
                    service_name=auth.plan.parameters.resource_name, service=response)
            else:
                snapshot = _deployment_snapshot(namespace=auth.plan.parameters.namespace,
                    deployment_name=auth.plan.parameters.resource_name, deployment=response,
                    container_name=auth.plan.parameters.container_name)
            snapshot.uid = response.metadata.uid
            snapshot.generation = getattr(response.metadata, "generation", None)
            after = snapshot.model_dump(mode="json")
        except Exception:
            self.repository.record(self.lease, "outcome_unknown", code="INVALID_WRITE_RESPONSE")
            raise OutcomeUnknown("patch response cannot be attributed") from None
        if (after["uid"] != before["uid"] or after["configuration"] != target["configuration"] or
                (auth.plan.parameters.resource_kind == "Deployment" and type(after["generation"]) is not int) or
                not after["resource_version"] or after["resource_version"] == before["resource_version"]):
            self.repository.record(self.lease, "outcome_unknown", response=after, code="WRITE_RESPONSE_MISMATCH")
            raise OutcomeUnknown("patch response cannot be attributed")
        return self._result(auth, operation, "succeeded", after=after)

    def _result(self, auth, operation, status, *, after=None, code=None):
        now = datetime.now(UTC).isoformat()
        p = auth.plan.parameters
        result = ActionExecutionResult(execution_id=auth.execution_id, approval_id=auth.approval_id,
            action=auth.plan.action, status=status, namespace=p.namespace, resource_kind=p.resource_kind,
            resource_name=p.resource_name, started_at=operation["created_at"].isoformat(), finished_at=now,
            before_snapshot=ResourceSnapshot.model_validate(operation["before_snapshot"]),
            after_snapshot=ResourceSnapshot.model_validate(after) if after else None,
            applied_patch={"json_patch": operation["request_patch"]} if status == "succeeded" else {},
            rollback_patch={}, message="Recorded Kubernetes patch response." if after else "Write rejected before application.",
            error_code=code)
        self.repository.record(self.lease, "succeeded" if status == "succeeded" else "rejected",
            response=after, result=result.model_dump(mode="json"), code=code,
            attribution="confirmed" if status == "succeeded" else "not_established")
        report("operation_result", self.lease, operation_id=operation["operation_id"], error_code=code,
               output_value=result.model_dump(mode="json"))
        hit("after_operation_result", self.lease, operation["operation_id"])
        return result

    def reconcile(self, operation):
        # Nothing here can issue a mutation. A matching current value without a
        # durable, attributable response is still manual_required.
        before, target = operation["before_snapshot"], operation["target_snapshot"]
        parameters = SimpleNamespace(namespace=before["namespace"], resource_kind=before["resource_kind"],
            resource_name=before["resource_name"], container_name=target["configuration"].get("container_name"))
        try:
            _, observed = read_target(self.clients, parameters)
        except Exception:
            self.repository.record(self.lease, "manual_required", code="RECONCILIATION_READ_FAILED")
            return False
        response = operation["response_snapshot"]
        confirmed = (operation["attribution"] == "confirmed" and response is not None and
                     observed == response and observed["configuration"] == target["configuration"] and
                     observed["uid"] == before["uid"] and operation["result"] is not None)
        self.repository.record(self.lease, "reconciled" if confirmed else "manual_required", observed=observed,
            attribution="confirmed" if confirmed else "not_established",
            code=None if confirmed else "OPERATION_MANUAL_REQUIRED")
        report("operation_reconciled", self.lease, operation_id=operation["operation_id"],
               error_class=None if confirmed else "outcome_unknown", output_value=observed)
        return confirmed
