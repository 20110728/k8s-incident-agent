"""4A-2: isolated PostgreSQL, checkpoints, worker and API acceptance."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import os
import json
import subprocess
import sys

import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.graph import StateGraph, START, END

from backend.app.agent.state import IncidentState
from backend.app.agent.approval import build_approval_request, validate_approval_decision, InvalidApprovalDecision
from backend.app.api.dependencies import build_incident_service
from backend.app.api.routes.rounds import get_round_repository
from backend.app.config import ApiSettings
from backend.app.main import create_app
from backend.app.persistence.checkpointer import postgres_checkpointer
from backend.app.persistence.incidents import PostgresIncidentRepository
from backend.app.persistence.messages import PostgresMessageRepository
from backend.app.persistence.rounds import RoundRepository, RoundConflict, RoundNotFound
from backend.app.persistence.runs import IdempotencyConflict, RunError
from backend.app.llm.context_builder import build_diagnosis_context, MAX_TOTAL_CONTEXT_CHARACTERS
from backend.app.runtime.worker import Worker
from backend.app.runtime.settings import WorkerSettings
from backend.app.runtime.checkpointer import fenced_checkpointer
from backend.app.runtime.recovery import classify
from backend.app.services.message_schemas import MessageDraft
from backend.app.services.round_context import build_round_context
from backend.tests.messages.test_messages import legacy
from backend.tests.runtime.test_worker_postgres import storage, accept, expire
from backend.tests.runtime.test_operations_postgres import state_with_uid, decision_for


def workflow(saver):
    builder = StateGraph(IncidentState)
    builder.add_node("diagnose_incident", lambda state: {"phase": "remediation_skipped"})
    builder.add_edge(START, "diagnose_incident")
    builder.add_edge("diagnose_incident", END)
    return builder.compile(checkpointer=saver)


def worker_for(connect, settings):
    repo = RoundRepository(connect)
    @contextmanager
    def graph_context(lease, lost):
        with fenced_checkpointer(settings, repo, lease, lost) as saver:
            yield workflow(saver)
    return Worker(repo, graph_context, WorkerSettings(_env_file=None, heartbeat_seconds=1,
                  lease_seconds=30, poll_seconds=0.1, shutdown_seconds=1))


def message(connect, incident, key="note"):
    return PostgresMessageRepository(connect).append(incident,
        MessageDraft(client_message_id=key, content="Reported after release; not observed evidence")).message


def seed(storage):
    connect, _, settings = storage
    incident = legacy(connect)
    previous = {"incident_id": incident, "request": {"namespace": "agent-demo", "service_name": "order-service",
                "description": "historical incident"}, "phase": "remediation_skipped"}
    with postgres_checkpointer(settings) as saver:
        workflow(saver).invoke(previous, {"configurable": {"thread_id": incident}})
    return incident, previous, message(connect, incident)


def test_4A2_transactional_acceptance_and_idempotency(storage):
    connect, _, _ = storage
    incident, previous, note = seed(storage)
    repo = RoundRepository(connect)
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda _: repo.accept_round(incident, note.message_id, "same", None, previous), range(12)))
    assert len({r["run_id"] for r in rows}) == 1
    row = rows[0]
    assert row["thread_id"] != incident and row["source_message_id"] == note.message_id
    assert row["input_message_sequence"] == note.sequence
    assert row["approval_payload"] is None and row["checkpoint_started"] is False
    assert row["context_snapshot"]["messages"][0]["source"] == "user_supplied"
    with pytest.raises(IdempotencyConflict):
        repo.accept_round(incident, "other", "same", None, previous)
    with pytest.raises(RoundConflict):
        repo.accept_round(incident, note.message_id, "another-key", None, previous)
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM incident_agent_app.runs").fetchone()["n"] == 1
        assert conn.execute("SELECT related_run_id FROM incident_agent_app.messages").fetchone()["related_run_id"] is None


def test_4A2_worker_rounds_and_old_history_are_isolated(storage):
    connect, _, settings = storage
    incident, previous, note = seed(storage)
    repo = RoundRepository(connect)
    first = repo.accept_round(incident, note.message_id, "first", None, previous)
    worker_for(connect, settings).run(once=True)
    first = repo.get_round(incident, first["run_id"])
    assert first["status"] == "succeeded"
    frozen = deepcopy(first["output_snapshot"])
    assert frozen["run_id"] == first["run_id"]
    assert frozen["round_context"] == first["context_snapshot"]
    second_note = message(connect, incident, "second")
    second = repo.accept_round(incident, second_note.message_id, "second", first["run_id"], frozen)
    assert second["thread_id"] != first["thread_id"] and second["input_revision"] == 2
    worker_for(connect, settings).run(once=True)
    assert repo.get_round(incident, second["run_id"])["status"] == "succeeded"
    assert repo.get_round(incident, first["run_id"])["output_snapshot"] == frozen
    with postgres_checkpointer(settings) as saver:
        old = workflow(saver).get_state({"configurable": {"thread_id": incident}}).values
        assert "run_id" not in old and "round_context" not in old
        assert old["request"] == previous["request"]
        service = build_incident_service(checkpointer=saver, repository=PostgresIncidentRepository(connect),
                                         runs=repo, execution_mode="queued")
        assert service.get_run_snapshot(first).state == frozen
        assert service.get_incident(incident).run["run_id"] == second["run_id"]
    env = dict(os.environ, ROUND_DSN=settings.database_url.get_secret_value(), ROUND_INCIDENT=incident, ROUND_RUN=first["run_id"])
    code = """
import json,os,psycopg
from psycopg.rows import dict_row
from backend.app.persistence.rounds import RoundRepository
repo=RoundRepository(lambda: psycopg.connect(os.environ['ROUND_DSN'],row_factory=dict_row))
print(json.dumps(repo.get_round(os.environ['ROUND_INCIDENT'],os.environ['ROUND_RUN'])['output_snapshot']))
"""
    process = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, "fresh process could not read historical round"
    assert json.loads(process.stdout) == frozen


def test_4A2_existing_v1_run_and_pending_approval_compatibility(storage):
    connect, _, settings = storage
    repo = RoundRepository(connect)
    old = accept(repo)
    worker_for(connect, settings).run(once=True)
    assert repo.get_round(old["incident_id"], old["run_id"])["status"] == "succeeded"
    with postgres_checkpointer(settings) as saver:
        previous = workflow(saver).get_state({"configurable": {"thread_id": old["thread_id"]}}).values
    note = message(connect, old["incident_id"])
    new = repo.accept_round(old["incident_id"], note.message_id, "next", old["run_id"], previous)
    assert new["parent_run_id"] == old["run_id"]
    assert repo.get_round(old["incident_id"], old["run_id"])["output_snapshot"] == previous
    with pytest.raises(RoundConflict):
        repo.accept_round(old["incident_id"], note.message_id, "busy", new["run_id"], previous)
    pending = legacy(connect)
    pending_note = message(connect, pending)
    with pytest.raises(RoundConflict):
        repo.accept_round(pending, pending_note.message_id, "pending", None,
                          {"phase": "awaiting_approval", "approval_status": "pending"})
    state = state_with_uid()
    old_approval = build_approval_request(state)
    state["run_id"] = new["run_id"]
    new_approval = build_approval_request(state)
    assert old_approval.approval_id != new_approval.approval_id
    decision = {**decision_for(state), "approval_id": old_approval.approval_id}
    with pytest.raises(InvalidApprovalDecision):
        validate_approval_decision(decision, new_approval)


def test_4A2_context_bounds_and_recovery_integrity(storage):
    connect, _, settings = storage
    incident, previous, note = seed(storage)
    repo = RoundRepository(connect)
    data = [note.model_dump()] * 11
    data = [{**row, "content": "password=secret " + "x" * 2000} for row in data]
    bounded = build_round_context(data, previous, None, incident)
    assert bounded["older_messages_omitted"] and len(bounded["messages"]) == 10
    assert all(len(m["content"]) <= 600 and m["truncated"] for m in bounded["messages"])
    assert "secret" not in json.dumps(bounded)
    prompt = build_diagnosis_context({"request": previous["request"], "round_context": bounded})
    assert "NOT current evidence or instructions" in prompt
    assert len(prompt) <= MAX_TOTAL_CONTEXT_CHARACTERS
    row = repo.accept_round(incident, note.message_id, "new", None, previous)
    claimed = repo.claim("lost-worker", 30)
    expire(connect, claimed["run_id"])
    worker_for(connect, settings).run(once=True)
    finished = repo.get_round(incident, row["run_id"])
    assert finished["status"] == "succeeded" and finished["attempt"] == 2
    snapshot = SimpleNamespace(values=finished["output_snapshot"], next=(), tasks=(), config={"configurable": {"checkpoint_id": "saved"}})
    assert classify(finished, snapshot).status == "succeeded"
    corrupt = {**finished, "context_sha256": "bad"}
    assert classify(corrupt, snapshot).error_code == "UNSUPPORTED_OR_CORRUPT_INPUT"
    snapshot.values = {**snapshot.values, "run_id": "wrong-round"}
    assert classify(finished, snapshot).error_code == "CHECKPOINT_INPUT_MISMATCH"


def test_4A2_api_explicit_round_and_history(storage):
    connect, _, settings = storage
    incident, _, note = seed(storage)
    repo = RoundRepository(connect)
    with postgres_checkpointer(settings) as saver:
        service = build_incident_service(checkpointer=saver, repository=PostgresIncidentRepository(connect),
                                         runs=repo, execution_mode="queued")
        app = create_app(ApiSettings(_env_file=None, environment="test", execution_mode="queued"))
        app.state.incident_service = service
        app.dependency_overrides[get_round_repository] = lambda: repo
        with TestClient(app) as client:
            url = f"/api/v1/incidents/{incident}/runs"
            body = {"client_request_id": "api", "message_id": note.message_id}
            first = client.post(url, json=body)
            assert first.status_code == 202, first.text
            run_id = first.json()["run"]["run_id"]
            assert client.post(url, json=body).json()["run"]["run_id"] == run_id
            assert client.get(url + "/" + run_id).status_code == 200
            assert client.get(url + "/legacy").status_code == 200
            assert client.get(url + "/missing").status_code == 404
            assert client.post(url, json={**body, "message_id": "changed"}).status_code == 409
            assert client.post(url, json={**body, "namespace": "another"}).status_code == 422
            assert repo.get_round(incident, run_id)["attempt"] == 0  # HTTP never invoked a graph.


def test_4A2_cross_incident_input_and_stale_parent_rejected(storage):
    connect, _, _ = storage
    incident, previous, note = seed(storage)
    other = legacy(connect)
    other_note = message(connect, other)
    repo = RoundRepository(connect)
    with pytest.raises(RoundNotFound):
        repo.accept_round(incident, other_note.message_id, "cross", None, previous)
    with pytest.raises(RoundConflict):
        repo.accept_round(incident, note.message_id, "stale", "wrong-parent", previous)
    assert repo.latest(incident) is None
    @contextmanager
    def failed_commit():
        with connect() as conn:
            yield conn
            raise psycopg.OperationalError("simulated commit failure")
    with pytest.raises(RunError):
        RoundRepository(failed_commit).accept_round(incident, note.message_id, "retry", None, previous)
    assert repo.latest(incident) is None
    with connect() as conn:
        assert conn.execute("SELECT phase FROM incident_agent_app.incidents WHERE incident_id=%s",
                            (incident,)).fetchone()["phase"] == "remediation_skipped"
    assert repo.accept_round(incident, note.message_id, "retry", None, previous)["status"] == "queued"
