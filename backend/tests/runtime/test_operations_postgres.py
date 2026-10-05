"""Real journal transactions + controlled Kubernetes responses; ECS only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace as NS
from uuid import uuid4
import threading

import pytest
from kubernetes.client.exceptions import ApiException
from langgraph.graph import StateGraph, START, END

from backend.app.agent.approval import build_approval_request
from backend.app.agent.graph import route_after_approval, route_after_execution
from backend.app.agent.nodes import request_human_approval, make_execute_remediation_node
from backend.app.agent.schemas import ApprovalDecision
from backend.app.agent.state import IncidentState
from backend.app.persistence.incidents import PostgresIncidentRepository
from backend.app.persistence.leases import LeaseLost
from backend.app.persistence.operations import ApprovalConflict, approval_binding, json_value
from backend.app.runtime import operations as module
from backend.app.runtime.operations import LedgerExecutor, OutcomeUnknown
from backend.app.runtime.checkpointer import fenced_checkpointer
from backend.app.runtime.worker import Worker
from backend.app.services.incident_service import IncidentApplicationService, IncidentApprovalConflictError
from backend.tests.agent.test_execution_policy import approved_state
from backend.tests.api.fakes import FakeIncidentGraph
from backend.tests.runtime.test_worker_postgres import storage, expire


class ProcessStopped(BaseException):
    """An abrupt stop bypasses the workflow's Exception handlers."""


class Kubernetes:
    def __init__(self):
        self.resource = NS(metadata=NS(uid="service-uid", resource_version="10", generation=None),
                           spec=NS(selector={"app": "wrong-service"}))
        self.calls = []
        self.mode = "success"
        self.clients = NS(core=self, apps=self)

    def read_namespaced_service(self, **kwargs):
        return deepcopy(self.resource)

    def patch_namespaced_service(self, *, name, namespace, body, _request_timeout):
        self.calls.append(deepcopy(body))
        assert body[0] == {"op": "test", "path": "/metadata/uid", "value": "service-uid"}
        assert body[1]["path"] == "/metadata/resourceVersion"
        assert body[2]["path"] == "/spec/selector"
        assert body[3]["op"] == "replace"
        if self.mode == "reject":
            raise ApiException(status=422)
        if self.mode == "timeout_old":
            raise TimeoutError()
        self.resource.spec.selector = deepcopy(body[3]["value"])
        self.resource.metadata.resource_version = "11"
        if self.mode == "timeout_applied":
            raise TimeoutError()
        return deepcopy(self.resource)


def state_with_uid():
    state = json_value(approved_state())
    state["evidence"][0]["data"]["uid"] = "service-uid"
    state["approval_request"] = build_approval_request(state).model_dump(mode="json")
    state["approval_record"]["approval_id"] = state["approval_request"]["approval_id"]
    return state


def decision_for(state, approved=True):
    return {key: state["approval_record"][key] for key in ("approval_id", "approver", "comment")} | {"approved": approved}


def waiting(repo, state):
    row = repo.accept(incident_id=state["incident_id"], run_id=str(uuid4()), thread_id=str(uuid4()),
                      payload=state["request"], key=None)
    lease = repo.claim("investigation", 60)
    repo.finish(lease, "waiting_approval", phase="awaiting_approval")
    return row


@pytest.fixture
def operation_case(storage, monkeypatch):
    connect, repo, _ = storage
    state = state_with_uid()
    row = waiting(repo, state)
    repo.queue_approval(row["run_id"], decision_for(state), approval_binding(state))
    lease = repo.claim("executor", 60)
    kube = Kubernetes()
    # Authorization is real; the live release-profile reader is controlled here.
    monkeypatch.setattr(module, "revalidate_live_profile", lambda *args: {"resource_version": "10"})
    return connect, repo, state, lease, kube


def test_response_is_durable_and_replay_never_patches_twice(operation_case):
    _, repo, state, lease, kube = operation_case
    executor = LedgerExecutor(kube.clients, repo, lease)
    first = executor.execute(state)
    operation = repo.operation(lease["run_id"])
    assert operation["state"] == "succeeded"
    assert operation["response_snapshot"]["resource_version"] == "11"
    assert operation["response_snapshot"]["uid"] == "service-uid"
    assert operation["plan_snapshot"] == state["remediation_plan"]
    assert operation["approval_snapshot"] == lease["approval_payload"]
    assert executor.execute(state) == first
    assert len(kube.calls) == 1
    assert repo.operation(lease["run_id"])["operation_id"] == operation["operation_id"]


def test_crash_while_prepared_reuses_id_before_first_patch(operation_case, monkeypatch):
    connect, repo, state, lease, kube = operation_case
    original = repo.dispatch
    def stopped(*args):
        raise ProcessStopped()
    monkeypatch.setattr(repo, "dispatch", stopped)
    with pytest.raises(ProcessStopped):
        LedgerExecutor(kube.clients, repo, lease).execute(state)
    before = repo.operation(lease["run_id"])
    assert before["state"] == "prepared" and not kube.calls
    monkeypatch.setattr(repo, "dispatch", original)
    expire(connect, lease["run_id"])
    replacement = repo.claim("replacement", 60)
    result = LedgerExecutor(kube.clients, repo, replacement).execute(state)
    assert result.status == "succeeded" and len(kube.calls) == 1
    assert repo.operation(lease["run_id"])["operation_id"] == before["operation_id"]
    with pytest.raises(LeaseLost):
        repo.record(lease, "manual_required")


@pytest.mark.parametrize("mode,change", [("timeout_old", None), ("timeout_applied", None),
                                        ("timeout_applied", "third_value"), ("timeout_applied", "recreated")])
def test_unknown_response_never_authorizes_another_patch(operation_case, mode, change):
    connect, repo, state, lease, kube = operation_case
    kube.mode = mode
    with pytest.raises(OutcomeUnknown):
        LedgerExecutor(kube.clients, repo, lease).execute(state)
    if change == "third_value":
        kube.resource.spec.selector = {"app": "somebody-else"}
    if change == "recreated":
        kube.resource.metadata.uid = "replacement-uid"
    expire(connect, lease["run_id"])
    replacement = repo.claim("replacement", 60)
    executor = LedgerExecutor(kube.clients, repo, replacement)
    assert executor.reconcile(repo.operation(lease["run_id"])) is False
    operation = repo.operation(lease["run_id"])
    assert operation["state"] == "manual_required"
    assert operation["attribution"] == "not_established"
    assert operation["observed_snapshot"]["uid"] == kube.resource.metadata.uid
    with pytest.raises(OutcomeUnknown):
        executor.execute(state)
    assert len(kube.calls) == 1


def test_response_before_database_commit_is_not_treated_as_success(operation_case, monkeypatch):
    connect, repo, state, lease, kube = operation_case
    original = repo.record
    monkeypatch.setattr(repo, "record", lambda *args, **kwargs: (_ for _ in ()).throw(ProcessStopped()))
    with pytest.raises(ProcessStopped):
        LedgerExecutor(kube.clients, repo, lease).execute(state)
    assert repo.operation(lease["run_id"])["state"] == "dispatching"
    monkeypatch.setattr(repo, "record", original)
    expire(connect, lease["run_id"])
    def forbidden_graph(*args):
        pytest.fail("unknown operation must be reconciled before loading the graph")
    worker = Worker(repo, forbidden_graph, reconciler=lambda row, op: LedgerExecutor(kube.clients, repo, row).reconcile(op))
    worker.run(once=True)
    assert repo.latest(lease["incident_id"])["status"] == "reconciling"
    assert repo.operation(lease["run_id"])["state"] == "manual_required"
    assert repo.claim("next", 60) is None
    assert len(kube.calls) == 1


@pytest.mark.parametrize("change", [None, "resource_version", "uid", "configuration"])
def test_known_response_requires_exact_observation_on_recovery(operation_case, change):
    connect, repo, state, lease, kube = operation_case
    LedgerExecutor(kube.clients, repo, lease).execute(state)
    if change == "configuration":
        kube.resource.spec.selector = {"app": "other"}
    elif change:
        setattr(kube.resource.metadata, change, "changed")
    expire(connect, lease["run_id"])
    replacement = repo.claim("replacement", 60)
    confirmed = LedgerExecutor(kube.clients, repo, replacement).reconcile(repo.operation(lease["run_id"]))
    assert confirmed is (change is None)
    assert repo.operation(lease["run_id"])["state"] == ("reconciled" if confirmed else "manual_required")
    assert len(kube.calls) == 1


@pytest.mark.parametrize("change", ["uid", "selector", "binding", "approver", "lease"])
def test_write_preconditions_fail_closed(operation_case, change):
    connect, repo, state, lease, kube = operation_case
    if change == "uid":
        kube.resource.metadata.uid = "replacement"
    elif change == "selector":
        kube.resource.spec.selector = {"app": "other"}
    elif change == "binding":
        lease["approval_payload"]["binding"] = "changed"
    elif change == "approver":
        lease["approval_payload"]["decision"]["approver"] = "changed"
    else:
        expire(connect, lease["run_id"])
    executor = LedgerExecutor(kube.clients, repo, lease)
    if change in {"uid", "selector"}:
        assert executor.execute(state).status == "conflict"
    else:
        with pytest.raises((ValueError, LeaseLost)):
            executor.execute(state)
    assert not kube.calls


def test_rejected_response_is_not_retried(operation_case):
    _, repo, state, lease, kube = operation_case
    kube.mode = "reject"
    executor = LedgerExecutor(kube.clients, repo, lease)
    assert executor.execute(state).status == "conflict"
    assert executor.execute(state).status == "conflict"
    assert repo.operation(lease["run_id"])["state"] == "rejected" and len(kube.calls) == 1


def test_heartbeat_failure_during_preparation_prevents_dispatch(operation_case, monkeypatch):
    _, repo, state, lease, kube = operation_case
    lost = threading.Event()
    original = repo.dispatch
    def interrupted(*args):
        original(*args)
        lost.set()
    monkeypatch.setattr(repo, "dispatch", interrupted)
    with pytest.raises(LeaseLost):
        LedgerExecutor(kube.clients, repo, lease, lost).execute(state)
    assert not kube.calls
    assert repo.operation(lease["run_id"])["state"] == "dispatching"


def test_prepared_snapshot_cannot_float_to_a_new_version(operation_case, monkeypatch):
    _, repo, state, lease, kube = operation_case
    original = repo.dispatch
    monkeypatch.setattr(repo, "dispatch", lambda *args: (_ for _ in ()).throw(ProcessStopped()))
    with pytest.raises(ProcessStopped):
        LedgerExecutor(kube.clients, repo, lease).execute(state)
    monkeypatch.setattr(repo, "dispatch", original)
    kube.resource.metadata.resource_version = "12"
    assert LedgerExecutor(kube.clients, repo, lease).execute(state).status == "conflict"
    assert not kube.calls


def test_response_committed_before_checkpoint_replays_result_only(operation_case, storage):
    connect, repo, state, lease, kube = operation_case
    settings = storage[2]
    @contextmanager
    def context(row, lost):
        with fenced_checkpointer(settings, repo, row, lost) as saver:
            builder = StateGraph(IncidentState)
            builder.add_node("execute_remediation", make_execute_remediation_node(LedgerExecutor(kube.clients, repo, row)))
            builder.add_node("verify_recovery", lambda state: {"phase": "verification_succeeded"})
            builder.add_edge(START, "execute_remediation")
            builder.add_conditional_edges("execute_remediation", route_after_execution, {"verify": "verify_recovery", "stop": END})
            builder.add_edge("verify_recovery", END)
            yield builder.compile(checkpointer=saver)
    config = {"configurable": {"thread_id": lease["thread_id"]}}
    with context(lease, threading.Event()) as graph:
        graph.invoke(state, config=config, interrupt_before=["execute_remediation"])
        assert graph.get_state(config).next == ("execute_remediation",)
    LedgerExecutor(kube.clients, repo, lease).execute(state)
    expire(connect, lease["run_id"])
    Worker(repo, context, reconciler=lambda row, op: LedgerExecutor(kube.clients, repo, row).reconcile(op)).run(once=True)
    result = repo.latest(lease["incident_id"])
    assert result["status"] == "succeeded", result["last_error"]
    assert repo.operation(lease["run_id"])["state"] == "reconciled"
    assert len(kube.calls) == 1


def test_approval_race_is_atomic_and_api_never_invokes_graph(storage):
    connect, repo, _ = storage
    state = state_with_uid()
    row = waiting(repo, state)
    graph = FakeIncidentGraph()
    pending = {**state, "phase": "awaiting_approval", "approval_status": "pending", "approved": None, "approval_record": None}
    graph.states[row["thread_id"]] = pending
    service = IncidentApplicationService(graph, PostgresIncidentRepository(connect), runs=repo, execution_mode="queued")
    def submit(approved):
        try:
            service.submit_approval(row["incident_id"], ApprovalDecision(**decision_for(state, approved)))
            return approved
        except (ApprovalConflict, IncidentApprovalConflictError):
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = [value for value in pool.map(submit, [True, False]) if value is not None]
    assert len(winners) == 1
    repeated = service.submit_approval(row["incident_id"], ApprovalDecision(**decision_for(state, winners[0])))
    assert repeated.run["status"] == "queued" and repeated.waiting_for_approval is False
    assert not graph.invocations and not graph.resume_calls


@pytest.mark.parametrize("approved,mode,expected", [(False, "success", "succeeded"),
    (True, "success", "succeeded"), (True, "timeout_applied", "reconciling")])
def test_worker_resumes_durable_approval_and_projects_journal(storage, monkeypatch, approved, mode, expected):
    connect, repo, settings = storage
    state = state_with_uid()
    pending = {**state, "phase": "awaiting_approval", "approval_status": "pending", "approved": None, "approval_record": None}
    row = repo.accept(incident_id=state["incident_id"], run_id=str(uuid4()), thread_id=str(uuid4()), payload=state["request"], key=None)
    kube = Kubernetes()
    kube.mode = mode
    monkeypatch.setattr(module, "revalidate_live_profile", lambda *args: {"resource_version": "10"})
    @contextmanager
    def context(lease, lost):
        with fenced_checkpointer(settings, repo, lease, lost) as saver:
            builder = StateGraph(IncidentState)
            builder.add_node("request_human_approval", request_human_approval)
            builder.add_node("execute_remediation", make_execute_remediation_node(LedgerExecutor(kube.clients, repo, lease)))
            builder.add_node("verify_recovery", lambda state: {"phase": "verification_succeeded"})
            builder.add_edge(START, "request_human_approval")
            builder.add_conditional_edges("request_human_approval", route_after_approval, {"execute": "execute_remediation", "stop": END})
            builder.add_conditional_edges("execute_remediation", route_after_execution, {"verify": "verify_recovery", "stop": END})
            builder.add_edge("verify_recovery", END)
            yield builder.compile(checkpointer=saver)
    lease = repo.claim("investigation", 60)
    with context(lease, threading.Event()) as graph:
        graph.invoke(pending, {"configurable": {"thread_id": row["thread_id"]}})
    repo.finish(lease, "waiting_approval")
    repo.queue_approval(row["run_id"], decision_for(state, approved), approval_binding(state))
    Worker(repo, context, reconciler=lambda row, op: LedgerExecutor(kube.clients, repo, row).reconcile(op)).run(once=True)
    result = repo.latest(row["incident_id"])
    assert result["status"] == expected, result["last_error"]
    assert len(kube.calls) == int(approved)
    assert repo.history(row["incident_id"]) == service_history(repo, row["incident_id"])


def service_history(repo, incident_id):
    return IncidentApplicationService(None, PostgresIncidentRepository(repo._connect), runs=repo).list_operations(incident_id)["items"]
