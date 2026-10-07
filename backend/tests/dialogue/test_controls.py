"""4B-2 acceptance: real PostgreSQL/checkpoints, worker processes and dispatch fencing."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
import json
import os
import subprocess
import sys
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.graph import StateGraph, START, END

from backend.app.agent.clarification import prepare_clarification, wire_clarification
from backend.app.agent.graph import route_after_diagnosis, build_incident_graph
from backend.app.agent.state import IncidentState
from backend.app.api.dependencies import build_incident_service
from backend.app.api.routes.controls import get_control_repository
from backend.app.api.routes.interactions import get_interaction_repository
from backend.app.config import ApiSettings
from backend.app.main import create_app
from backend.app.persistence.checkpointer import postgres_checkpointer
from backend.app.persistence.controls import ControlRepository
from backend.app.persistence.database import connect_database
from backend.app.persistence.incidents import PostgresIncidentRepository
from backend.app.persistence.leases import LeaseLost
from backend.app.persistence.messages import PostgresMessageRepository
from backend.app.persistence.operations import ApprovalConflict, approval_binding
from backend.app.persistence.rounds import RoundConflict
from backend.app.persistence.runs import IdempotencyConflict
from backend.app.persistence.settings import DatabaseSettings
from backend.app.runtime.checkpointer import fenced_checkpointer
from backend.app.runtime.operations import LedgerExecutor, OutcomeUnknown
from backend.app.runtime.settings import WorkerSettings
from backend.app.runtime.recovery import classify
from backend.app.runtime.worker import Worker
from backend.app.services.control_schemas import ControlRequest, AnswerQuestion
from backend.app.services.interaction_schemas import CreateInteraction
from backend.app.services.round_context import DIALOGUE_WORKFLOW
from backend.tests.runtime.test_worker_postgres import storage, accept
from backend.tests.runtime.test_operations_postgres import operation_case, state_with_uid, decision_for, waiting
from backend.tests.interactions.test_interactions import FakeModel, run_worker, reference


def dialogue_graph(saver):
    builder = StateGraph(IncidentState)
    def collect(state):
        version = state.get("clarification_round", 0)
        return {"phase": "evidence_collected", "evidence": [{"evidence_id": "sample-" + str(version),
                "collected_at": datetime.now(UTC).isoformat(), "source": "controlled_collector"}]}
    def diagnose(state):
        missing = (["故障开始时间", "近期发布变更"] if state.get("clarification_round", 0) == 0
                   else ["具体报错内容", "影响范围"])
        return {"phase": "diagnosis_completed", "diagnosis": {"fault_category": "unknown",
                "root_cause": "Human context is missing", "evidence_ids": [state["evidence"][0]["evidence_id"]],
                "runbook_ids": [], "confidence": 0.0, "reasoning_summary": "Insufficient observed evidence",
                "assessment": {"schema_version": "v2", "problem_domain": "insufficient_evidence", "symptoms": [],
                    "root_cause_hypotheses": [], "missing_evidence": missing, "next_investigation": [],
                    "resource_status": "unknown", "business_status": "unknown", "unverified_scope": ["current incident"]}}}
    builder.add_node("collect_evidence", collect)
    builder.add_node("diagnose_incident", diagnose)
    builder.add_node("skip_remediation", lambda _: {"phase": "remediation_skipped"})
    builder.add_node("plan_remediation", lambda _: (_ for _ in ()).throw(AssertionError("must not plan a write")))
    builder.add_edge(START, "collect_evidence")
    builder.add_edge("collect_evidence", "diagnose_incident")
    wire_clarification(builder, route_after_diagnosis)
    builder.add_edge("skip_remediation", END)
    builder.add_edge("plan_remediation", END)
    return builder.compile(checkpointer=saver)


def worker_for_dialogue(repo, settings):
    @contextmanager
    def context(lease, lost):
        with fenced_checkpointer(settings, repo, lease, lost) as saver:
            yield dialogue_graph(saver)
    return Worker(repo, context, WorkerSettings(_env_file=None, lease_seconds=30, heartbeat_seconds=1,
                  poll_seconds=0.1, shutdown_seconds=1))


def new_dialogue(storage):
    repo = ControlRepository(storage[0])
    row = repo.accept(incident_id=str(uuid4()), run_id=str(uuid4()), thread_id=str(uuid4()),
        payload={"namespace": "agent-demo", "service_name": "order-service", "description": "unknown incident"},
        key=None, dialogue=True)
    worker_for_dialogue(repo, storage[2]).run(once=True)
    return repo, repo.latest(row["incident_id"])


def snapshot(storage, row):
    with postgres_checkpointer(storage[2]) as saver:
        return dialogue_graph(saver).get_state({"configurable": {"thread_id": row["thread_id"]}}).values


def answer_for(row, key="answer", skip=False):
    question = row["question_payload"]
    return AnswerQuestion(client_message_id=key, content="operator reply", question_id=question["question_id"],
        version=question["version"], skip=skip, answers={} if skip else {q["slot"]: "user supplied detail" for q in question["questions"]})


def control(action, key=None):
    return ControlRequest(action=action, client_message_id=key or action, content="operator " + action)


def test_4B2_questions_survive_process_restart_and_recollect(storage):
    repo, first = new_dialogue(storage)
    assert first["workflow_version"] == DIALOGUE_WORKFLOW
    assert first["status"] == "waiting_user" and first["lease_owner"] is None
    question = first["question_payload"]
    assert len(question["questions"]) == 2 and question["version"] == 1
    assert repo.claim("idle", 30) is None
    receipt = repo.answer(first["incident_id"], first["run_id"], answer_for(first))
    accepted = repo.get_round(first["incident_id"], first["run_id"])
    assert repo.get_round(first["incident_id"], first["run_id"])["adopted_message_ids"] == []
    env = dict(os.environ, CONTROL_TEST_DSN=storage[2].database_url.get_secret_value())
    code = """
import os
from functools import partial
from backend.app.persistence.database import connect_database
from backend.app.persistence.settings import DatabaseSettings
from backend.app.persistence.controls import ControlRepository
from backend.tests.dialogue.test_controls import worker_for_dialogue
settings=DatabaseSettings(database_url=os.environ['CONTROL_TEST_DSN'])
worker_for_dialogue(ControlRepository(partial(connect_database,settings)),settings).run(once=True)
"""
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    second = repo.latest(first["incident_id"])
    assert second["run_id"] == first["run_id"] and second["thread_id"] == first["thread_id"]
    assert second["status"] == "waiting_user" and second["question_payload"]["version"] == 2
    assert second["question_payload"]["question_id"] != question["question_id"]
    assert second["question_payload"]["evidence_revision"] != question["evidence_revision"]
    # A crash before projecting the second wait leaves the first answer on the
    # lease; its durable adoption must prevent a replay/version-mismatch failure.
    with postgres_checkpointer(storage[2]) as saver:
        checkpoint = dialogue_graph(saver).get_state({"configurable": {"thread_id": first["thread_id"]}})
        recovery = classify(accepted, checkpoint)
    assert recovery.action == "stop" and recovery.status == "waiting_user"
    assert receipt["message_id"] in second["adopted_message_ids"]
    reply = PostgresMessageRepository(storage[0]).list(first["incident_id"]).items[0]
    assert reply.adopted_by_run_ids == [first["run_id"]]
    repo.answer(first["incident_id"], first["run_id"], answer_for(second, "answer-2"))
    worker_for_dialogue(repo, storage[2]).run(once=True)
    final = repo.latest(first["incident_id"])
    assert final["status"] == "succeeded" and final["question_payload"] is None
    assert final["output_snapshot"]["clarification_exhausted"]
    assert len(final["output_snapshot"]["asked_slots"]) == 4
    assert final["output_snapshot"]["evidence"][0]["evidence_id"] == "sample-2"
    assert len(final["output_snapshot"]["clarification_answers"]) == 2


def test_4B2_reply_is_idempotent_stale_replies_rejected_and_skip_finishes(storage):
    repo, row = new_dialogue(storage)
    body = answer_for(row, skip=True)
    wrong = body.model_copy(update={"question_id": "obsolete"})
    with pytest.raises(RoundConflict):
        repo.answer(row["incident_id"], row["run_id"], wrong)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: repo.answer(row["incident_id"], row["run_id"], body), range(12)))
    assert len({r["control_id"] for r in results}) == 1
    with pytest.raises(IdempotencyConflict):
        repo.answer(row["incident_id"], row["run_id"], body.model_copy(update={"content": "changed"}))
    with pytest.raises(RoundConflict):
        repo.answer(row["incident_id"], row["run_id"], body.model_copy(update={"client_message_id": "duplicate-new-key"}))
    worker_for_dialogue(repo, storage[2]).run(once=True)
    final = repo.latest(row["incident_id"])
    assert final["status"] == "succeeded" and final["output_snapshot"]["evidence"][0]["evidence_id"] == "sample-0"
    assert final["output_snapshot"]["diagnosis"]["fault_category"] == "unknown"
    assert repo.answer(row["incident_id"], row["run_id"], body)["control_id"] == results[0]["control_id"]


def test_4B2_explain_during_question_does_not_resume_or_invalidate(storage):
    repo, row = new_dialogue(storage)
    previous = snapshot(storage, row)
    task = repo.accept_interaction(row["incident_id"], CreateInteraction(client_message_id="why", content="why?", intent="explain"),
                                   row["run_id"], [reference(previous, row["run_id"])])
    run_worker(repo, model=FakeModel())
    current = repo.latest(row["incident_id"])
    assert current["status"] == "waiting_user" and current["question_payload"] == row["question_payload"]
    assert not current["stop_requested"]
    assert repo.get_round(row["incident_id"], task["run_id"])["status"] == "succeeded"


def test_4B2_stop_rejects_reply_and_continue_creates_a_new_run(storage):
    repo, row = new_dialogue(storage)
    previous = snapshot(storage, row)
    receipt = repo.control(row["incident_id"], control("stop"), row["run_id"], previous)
    stopped = repo.latest(row["incident_id"])
    assert stopped["status"] == "cancelled" and stopped["invalidated_at"]
    assert stopped["output_snapshot"] == previous
    with pytest.raises(RoundConflict):
        repo.answer(row["incident_id"], row["run_id"], answer_for(row))
    repo.control(row["incident_id"], control("investigate"), row["run_id"], previous)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: repo.activate_pending(), range(4)))
    new = repo.latest(row["incident_id"])
    assert new["run_id"] != row["run_id"] and new["thread_id"] != row["thread_id"]
    assert new["approval_payload"] is None and new["parent_run_id"] == row["run_id"]
    assert repo.find_control(row["incident_id"], "investigate")["diagnosis_run_id"] == new["run_id"]
    assert repo.control(row["incident_id"], control("stop"), row["run_id"], previous) == receipt


def test_4B2_answer_and_stop_race_cannot_resurrect_the_old_run(storage):
    repo, row = new_dialogue(storage)
    previous = snapshot(storage, row)
    gate = Barrier(2)
    def answer():
        gate.wait()
        try:
            return repo.answer(row["incident_id"], row["run_id"], answer_for(row))
        except RoundConflict:
            return None
    def stop():
        gate.wait()
        return repo.control(row["incident_id"], control("stop"), row["run_id"], previous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(answer), pool.submit(stop)
        a.result(timeout=15)
        b.result(timeout=15)
    assert repo.latest(row["incident_id"])["status"] == "cancelled"
    assert repo.claim("replacement", 30) is None


def test_4B2_new_facts_and_approval_are_serialized(storage):
    repo = ControlRepository(storage[0])
    for index in range(4):
        state = state_with_uid()
        state["incident_id"] = str(uuid4())
        row = waiting(repo, state)
        gate = Barrier(2)
        def approve():
            gate.wait()
            try:
                repo.queue_approval(row["run_id"], decision_for(state), approval_binding(state))
            except ApprovalConflict:
                pass
        def supplement():
            gate.wait()
            return repo.control(row["incident_id"], control("supplement", f"fact-{index}"), row["run_id"], state)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(approve), pool.submit(supplement)
            a.result(timeout=15)
            b.result(timeout=15)
        assert repo.latest(row["incident_id"])["status"] == "cancelled"
        with pytest.raises(ApprovalConflict):
            repo.queue_approval(row["run_id"], decision_for(state), approval_binding(state))


def test_4B2_supplement_before_dispatch_prevents_the_patch(operation_case, monkeypatch):
    connect, repo, state, lease, kube = operation_case
    controls = ControlRepository(connect)
    dispatch = repo.dispatch
    def change_before_dispatch(*args):
        controls.control(lease["incident_id"], control("supplement"), lease["run_id"], state)
        return dispatch(*args)
    monkeypatch.setattr(repo, "dispatch", change_before_dispatch)
    with pytest.raises((LeaseLost, ApprovalConflict)):
        LedgerExecutor(kube.clients, repo, lease).execute(state)
    assert not kube.calls and repo.operation(lease["run_id"])["state"] == "rejected"
    assert controls.latest(lease["incident_id"])["status"] == "cancelled"


@pytest.mark.parametrize("outcome", ["success", "timeout_applied"])
def test_4B2_dispatched_write_is_settled_before_pending_investigation(operation_case, monkeypatch, outcome):
    connect, repo, state, lease, kube = operation_case
    controls = ControlRepository(connect)
    dispatch = repo.dispatch
    def change_after_dispatch(*args):
        dispatch(*args)
        receipt = controls.control(lease["incident_id"], control("investigate"), lease["run_id"], state)
        assert receipt["deferred_until_write_checked"]
        controls.activate_pending()
        assert controls.latest(lease["incident_id"])["run_id"] == lease["run_id"]
    monkeypatch.setattr(repo, "dispatch", change_after_dispatch)
    kube.mode = outcome
    executor = LedgerExecutor(kube.clients, repo, lease)
    if outcome == "timeout_applied":
        with pytest.raises(OutcomeUnknown):
            executor.execute(state)
        assert executor.reconcile(repo.operation(lease["run_id"])) is False
        repo.finish(lease, "reconciling", error_code="OPERATION_MANUAL_REQUIRED")
        controls.activate_pending()
        assert controls.latest(lease["incident_id"])["run_id"] == lease["run_id"]
        assert controls.find_control(lease["incident_id"], "investigate")["status"] == "pending"
    else:
        result = executor.execute(state)
        assert result.status == "succeeded"
        previous = {**state, "phase": "remediation_executed", "action_result": result.model_dump(mode="json")}
        repo.finish(lease, "succeeded", phase="remediation_executed", output_snapshot=previous)
        controls.activate_pending()
        child = controls.latest(lease["incident_id"])
        assert child["run_id"] != lease["run_id"] and child["approval_payload"] is None
        assert controls.get_round(lease["incident_id"], lease["run_id"])["output_snapshot"] == previous
    assert len(kube.calls) == 1


def test_4B2_api_controls_are_findable_and_question_reads_need_no_model(storage):
    repo, row = new_dialogue(storage)
    with postgres_checkpointer(storage[2]) as saver:
        service = build_incident_service(checkpointer=saver, repository=PostgresIncidentRepository(storage[0]), runs=repo, execution_mode="queued")
        app = create_app(ApiSettings(_env_file=None, environment="test", execution_mode="queued"))
        app.state.incident_service = service
        app.dependency_overrides[get_control_repository] = lambda: repo
        app.dependency_overrides[get_interaction_repository] = lambda: repo
        with TestClient(app) as client:
            base = f"/api/v1/incidents/{row['incident_id']}"
            status = client.get(base + "/interaction-status")
            assert status.status_code == 200 and status.json()["run"]["question"] == row["question_payload"]
            # API graph must understand the new checkpoint nodes while remaining read-only.
            assert service.get_run_snapshot(row).state["question"] == row["question_payload"]
            request = {"client_message_id": "stop-api", "content": "先别查了", "intent": "auto"}
            stopped = client.post(base + "/interactions", json=request)
            assert stopped.status_code == 202, stopped.text
            assert stopped.json()["action"] == "stop"
            assert client.post(base + "/interactions", json=request).json() == stopped.json()
            assert client.get(base + "/interactions", params={"client_message_id": "stop-api"}).json() == stopped.json()
            displayed = client.get(base)
            assert displayed.status_code == 200, displayed.text
            assert displayed.json()["phase"] == "cancelled" and not displayed.json()["waiting_for_approval"]
            assert not displayed.json()["requires_approval"]
            assert client.post(base + f"/runs/{row['run_id']}/answers", json=answer_for(row).model_dump()).status_code == 409
            assert client.post(base + "/controls", json={**control("stop", "other").model_dump(), "namespace": "other"}).status_code == 422
            created = client.post("/api/v1/incidents", json={"namespace": "agent-demo", "service_name": "order-service", "description": "new incident"})
            assert created.status_code == 202, created.text
            assert repo.latest(created.json()["incident_id"])["workflow_version"] == DIALOGUE_WORKFLOW


def test_4B2_questions_only_cover_finite_human_information():
    dependencies = dict(collector=object(), retriever=object(), diagnoser=object(), planner=object())
    assert "await_user_input" not in build_incident_graph(**dependencies).get_graph().nodes
    assert "await_user_input" in build_incident_graph(**dependencies, dialogue=True).get_graph().nodes
    state = {"run_id": "run", "phase": "diagnosis_completed", "diagnosis": {"fault_category": "unknown",
             "assessment": {"missing_evidence": ["Pod logs", "EndpointSlice", "business probe"]}}}
    assert prepare_clarification(state)["question"] is None
    state["diagnosis"]["assessment"]["missing_evidence"] = ["故障开始时间", "发布变更", "具体报错", "影响范围"]
    first = prepare_clarification(state)
    assert len(first["question"]["questions"]) == 2
    assert prepare_clarification(state) == first  # Replay cannot manufacture a new question ID.
    state.update(first)
    state["phase"] = "diagnosis_completed"
    second = prepare_clarification(state)
    assert set(first["asked_slots"]).isdisjoint(q["slot"] for q in second["question"]["questions"])
    state.update(second)
    state["phase"] = "diagnosis_completed"
    assert prepare_clarification(state)["clarification_exhausted"]
