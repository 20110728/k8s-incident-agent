from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from backend.app.agent.approval import build_approval_request, create_approval_record
from backend.app.agent.execution_policy import validate_execution_authorization, InvalidExecutionAuthorization
from backend.app.agent.remediation_policy import prepare_remediation_plan, InvalidRemediationPlan
from backend.app.agent.schemas import ApprovalDecision, RemediationPlan
from backend.app.agent.target_identity import bind_target, plan_payload, revision, validate_relationships
from backend.app.tools.workload_tools import resolve_pod_owner
from backend.app.tools.service_tools import get_service_endpoint_slices
from backend.app.tools.remediation_tools import patch_service_selector
from backend.tests.runtime.test_operations_postgres import state_with_uid, decision_for


def bound_state():
    state = state_with_uid()
    plan, _ = prepare_remediation_plan(plan=RemediationPlan.model_validate(state["remediation_plan"]), state=state)
    state["remediation_plan"] = plan.model_dump(mode="json")
    request = build_approval_request(state)
    decision = ApprovalDecision(**{**decision_for(state), "approval_id": request.approval_id})
    state["approval_request"] = request.model_dump(mode="json")
    state["approval_record"] = create_approval_record(request, decision).model_dump(mode="json")
    return state


def test_new_plan_and_approval_revisions_bind_observed_uid():
    state = bound_state()
    auth = validate_execution_authorization(state)
    assert auth.plan.target_uid == "service-uid"
    assert auth.approval_request.plan_revision == revision(plan_payload(auth.plan))
    assert auth.approval_record.approval_revision == auth.approval_request.approval_revision
    assert build_approval_request(deepcopy(state)) == auth.approval_request


@pytest.mark.parametrize("change", ["uid", "plan", "profile", "evidence", "request", "record"])
def test_old_approval_cannot_authorize_changed_snapshot(change):
    state = bound_state()
    original = deepcopy(state["approval_record"])
    if change == "uid":
        state["evidence"][0]["data"]["uid"] = "recreated"
    elif change == "plan":
        state["remediation_plan"]["summary"] += " changed"
    elif change == "profile":
        state["service_profile"]["digest"] = "0" * 64
    elif change == "evidence":
        state["evidence"][0]["data"]["resource_version"] = "new-observation"
    elif change == "request":
        state["request"]["description"] += " new information"
    else:
        state["approval_record"]["plan_revision"] = "0" * 64
    with pytest.raises(InvalidExecutionAuthorization):
        validate_execution_authorization(state)
    if change != "record":
        assert state["approval_record"] == original


def test_missing_uid_or_model_invented_uid_cannot_be_bound():
    state = state_with_uid()
    plan = RemediationPlan.model_validate(state["remediation_plan"])
    with pytest.raises(ValueError, match="MISMATCH"):
        bind_target(plan.model_copy(update={"target_uid": "invented"}), state)
    del state["evidence"][0]["data"]["uid"]
    with pytest.raises(InvalidRemediationPlan, match="IDENTITY_MISSING"):
        prepare_remediation_plan(plan=plan, state=state)


def test_legacy_approval_fingerprint_is_stable():
    state = state_with_uid()
    plan = RemediationPlan.model_validate(state["remediation_plan"])
    payload = {"incident_id": state["incident_id"], "remediation_plan": plan_payload(plan),
               "service_profile": state["service_profile"], "diagnosis": state["diagnosis"], "evidence": state["evidence"]}
    request = build_approval_request(state)
    assert request.approval_id == "apr-" + revision(payload)[:16]
    assert request.plan_revision is None and request.approval_revision is None
    validate_execution_authorization(state)


@pytest.mark.parametrize("link", ["pod", "deployment", "service", "endpoint"])
def test_same_name_different_uid_is_not_a_valid_relationship(link):
    state = {"evidence": [
        {"resource_type": "Service", "resource_name": "svc", "data": {"uid": "svc-1"}},
        {"resource_type": "Deployment", "resource_name": "dep", "data": {"uid": "dep-1"}},
        {"resource_type": "PodStatus", "resource_name": "pod", "data": {"uid": "pod-1", "namespace": "agent-demo"}},
        {"resource_type": "OwnerChain", "resource_name": "pod", "data": {"owner_chain": {
            "pod_uid": "pod-1", "deployment_name": "dep", "deployment_uid": "dep-1"}}},
        {"resource_type": "EndpointSlice", "resource_name": "slice", "data": {"service_name": "svc", "service_uid": "svc-1",
            "endpoints": [{"target_kind": "Pod", "target_name": "pod", "target_uid": "pod-1", "target_namespace": "agent-demo"}]}},
    ]}
    validate_relationships(state)
    if link in {"pod", "deployment"}:
        state["evidence"][3]["data"]["owner_chain"][link + "_uid"] = "recreated"
    elif link == "service":
        state["evidence"][4]["data"]["service_uid"] = "recreated"
    else:
        state["evidence"][4]["data"]["endpoints"][0]["target_uid"] = "recreated"
    with pytest.raises(ValueError, match="IDENTITY"):
        validate_relationships(state)


def test_recreated_replicaset_does_not_resolve_by_name():
    owner = NS(kind="ReplicaSet", name="same-name", uid="old-rs", controller=True)
    pod = NS(metadata=NS(uid="pod-uid", owner_references=[owner]))
    replica = NS(metadata=NS(uid="new-rs", owner_references=[]))
    clients = NS(core=NS(read_namespaced_pod=lambda **kw: pod),
                 apps=NS(read_namespaced_replica_set=lambda **kw: replica))
    with pytest.raises(ValueError, match="RECREATED"):
        resolve_pod_owner(clients, "agent-demo", "pod")


def test_sync_writer_rejects_recreated_service_even_if_selector_matches():
    service = NS(metadata=NS(uid="new", resource_version="2"), spec=NS(selector={"app": "desired"}))
    calls = []
    clients = NS(core=NS(read_namespaced_service=lambda **kw: service,
                        patch_namespaced_service=lambda **kw: calls.append(kw)))
    result = patch_service_selector(clients=clients, namespace="agent-demo", service_name="svc",
        expected_selector={"app": "old"}, proposed_selector={"app": "desired"}, expected_uid="old")
    assert result.status == "conflict" and result.error_code == "TARGET_UID_CHANGED" and calls == []


def test_sync_patch_carries_approved_uid_and_observed_version():
    before = NS(metadata=NS(uid="same", resource_version="1"), spec=NS(selector={"app": "old"}))
    after = NS(metadata=NS(uid="same", resource_version="2"), spec=NS(selector={"app": "desired"}))
    calls = []
    def patch(**kwargs):
        calls.append(kwargs["body"])
        return after
    clients = NS(core=NS(read_namespaced_service=lambda **kw: before, patch_namespaced_service=patch))
    result = patch_service_selector(clients=clients, namespace="agent-demo", service_name="svc",
        expected_selector={"app": "old"}, proposed_selector={"app": "desired"}, expected_uid="same")
    assert result.status == "succeeded"
    assert calls[0]["metadata"] == {"uid": "same", "resourceVersion": "1"}
    assert result.after_snapshot.uid == "same"


def test_endpoint_collection_preserves_uid_links():
    endpoint = NS(addresses=["10.0.0.1"], conditions=None, node_name=None,
                  target_ref=NS(kind="Pod", name="pod", uid="pod-uid", namespace="agent-demo"))
    resource = NS(metadata=NS(name="slice", uid="slice-uid", resource_version="10",
                  owner_references=[NS(kind="Service", name="svc", uid="service-uid")]),
                  address_type="IPv4", endpoints=[endpoint])
    clients = NS(discovery=NS(list_namespaced_endpoint_slice=lambda **kw: NS(items=[resource])))
    result = get_service_endpoint_slices(clients, "agent-demo", "svc")[0]
    assert (result.uid, result.service_uid, result.endpoints[0].target_uid) == ("slice-uid", "service-uid", "pod-uid")
