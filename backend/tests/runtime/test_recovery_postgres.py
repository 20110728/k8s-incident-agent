import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from psycopg.types.json import Jsonb

from backend.app.agent.state import IncidentState
from backend.app.runtime.checkpointer import fenced_checkpointer
from backend.app.runtime.worker import Worker
from backend.tests.runtime.recovery_graph import context_factory, NODES
from backend.tests.runtime.test_worker_postgres import (
    storage, accept, short_settings, expire, wait_until, PAYLOAD,
)


def create_calls(connect):
    with connect() as connection:
        connection.execute("CREATE TABLE recovery_test_calls (node TEXT NOT NULL)")


def counts(connect):
    with connect() as connection:
        return {row["node"]: row["n"] for row in connection.execute(
            "SELECT node,count(*) AS n FROM recovery_test_calls GROUP BY node").fetchall()}


@pytest.mark.parametrize("blocked", ["retrieve_runbooks", "diagnose_incident", "plan_remediation"])
def test_kill_and_continue_only_pending_read_only_node(storage, blocked):
    connect, repo, settings = storage
    create_calls(connect)
    row = accept(repo)
    environment = dict(os.environ, PGVECTOR_URL=settings.database_url.get_secret_value(), RECOVERY_BLOCKED=blocked)
    code = """
import os
from functools import partial
from backend.app.persistence.database import connect_database
from backend.app.persistence.settings import get_database_settings
from backend.app.persistence.leases import LeaseRepository
from backend.app.runtime.worker import Worker
from backend.app.runtime.settings import WorkerSettings
from backend.tests.runtime.recovery_graph import context_factory
settings=get_database_settings()
connect=partial(connect_database,settings)
repo=LeaseRepository(connect)
Worker(repo,context_factory(connect,repo,settings,os.environ['RECOVERY_BLOCKED']),
       WorkerSettings(_env_file=None,lease_seconds=1,heartbeat_seconds=0.2)).run(once=True)
"""
    process = subprocess.Popen([sys.executable, "-c", code], env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_until(lambda: counts(connect).get(blocked) == 1, timeout=20)
        # Only assert no replay for work whose checkpoint has actually reached
        # PostgreSQL, not for an uncommitted in-memory node return.
        with context_factory(connect, repo, settings)(repo.latest(row["incident_id"]), threading.Event()) as saved_graph:
            wait_until(lambda: saved_graph.get_state(
                {"configurable": {"thread_id": row["thread_id"]}}).next == (blocked,))
        process.kill()
        process.wait(timeout=10)
        time.sleep(1.3)
        Worker(repo, context_factory(connect, repo, settings), short_settings()).run(once=True)
        result = repo.latest(row["incident_id"])
        assert result["status"] == "succeeded", result["last_error"]
        assert result["attempt"] == 2 and result["run_id"] == row["run_id"]
        assert counts(connect) == {name: 2 if name == blocked else 1 for name in NODES}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_saved_end_repairs_projection_even_at_attempt_limit(storage):
    connect, repo, settings = storage
    create_calls(connect)
    row = accept(repo)
    lease = repo.claim("old", 30)
    context = context_factory(connect, repo, settings)
    with context(lease, threading.Event()) as graph:
        graph.invoke({"incident_id": row["incident_id"], "request": PAYLOAD},
                     {"configurable": {"thread_id": row["thread_id"]}})
    with connect() as connection:
        connection.execute("UPDATE incident_agent_app.runs SET attempt=3 WHERE run_id=%s", (row["run_id"],))
    expire(connect, row["run_id"])
    Worker(repo, context, short_settings()).run(once=True)
    result = repo.latest(row["incident_id"])
    assert result["status"] == "succeeded" and result["attempt"] == 3
    assert counts(connect) == dict.fromkeys(NODES, 1)


def test_missing_checkpoint_after_write_intent_never_restarts(storage):
    connect, repo, settings = storage
    create_calls(connect)
    row = accept(repo)
    lease = repo.claim("old", 30)
    repo.mark_checkpoint_started(lease)
    expire(connect, row["run_id"])
    Worker(repo, context_factory(connect, repo, settings), short_settings()).run(once=True)
    result = repo.latest(row["incident_id"])
    assert result["last_error"]["code"] == "CHECKPOINT_MISSING"
    assert counts(connect) == {}


def test_expired_approval_checkpoint_restores_wait_without_resume(storage):
    connect, repo, settings = storage
    row = accept(repo)
    @contextmanager
    def context(lease, lost):
        with fenced_checkpointer(settings, repo, lease, lost) as saver:
            builder = StateGraph(IncidentState)
            builder.add_node("prepare_approval", lambda state: {"phase": "awaiting_approval", "approval_status": "pending"})
            def approval(state):
                interrupt({"kind": "approval"})
                raise AssertionError("must not resume without a saved decision")
            builder.add_node("request_human_approval", approval)
            builder.add_edge(START, "prepare_approval")
            builder.add_edge("prepare_approval", "request_human_approval")
            builder.add_edge("request_human_approval", END)
            yield builder.compile(checkpointer=saver)
    lease = repo.claim("old", 30)
    with context(lease, threading.Event()) as graph:
        graph.invoke({"incident_id": row["incident_id"], "request": PAYLOAD},
                     {"configurable": {"thread_id": row["thread_id"]}})
    expire(connect, row["run_id"])
    Worker(repo, context, short_settings()).run(once=True)
    assert repo.latest(row["incident_id"])["status"] == "waiting_approval"
    assert repo.claim("another", 30) is None


def test_transient_task_error_resumes_but_business_failed_end_does_not(storage):
    connect, repo, settings = storage
    create_calls(connect)
    row = accept(repo)
    @contextmanager
    def context(lease, lost):
        with fenced_checkpointer(settings, repo, lease, lost) as saver:
            builder = StateGraph(IncidentState)
            def diagnose(state):
                with connect() as connection:
                    connection.execute("INSERT INTO recovery_test_calls (node) VALUES ('diagnose_incident')")
                if counts(connect)["diagnose_incident"] == 1:
                    raise TimeoutError("synthetic dependency timeout")
                return {"phase": "diagnosis_failed"}
            builder.add_node("diagnose_incident", diagnose)
            builder.add_edge(START, "diagnose_incident")
            builder.add_edge("diagnose_incident", END)
            yield builder.compile(checkpointer=saver)
    Worker(repo, context, short_settings()).run(once=True)
    assert repo.latest(row["incident_id"])["status"] == "retry_scheduled"
    with connect() as connection:
        connection.execute("UPDATE incident_agent_app.runs SET next_retry_at=clock_timestamp()-interval '1 second' WHERE run_id=%s", (row["run_id"],))
    Worker(repo, context, short_settings()).run(once=True)
    result = repo.latest(row["incident_id"])
    assert result["status"] == "failed" and result["last_error"]["code"] == "WORKFLOW_FAILED"
    assert counts(connect)["diagnose_incident"] == 2
    assert repo.claim("another", 30) is None


def test_corrupt_checkpoint_stops_without_running_nodes(storage):
    connect, repo, settings = storage
    create_calls(connect)
    row = accept(repo)
    lease = repo.claim("old", 30)
    context = context_factory(connect, repo, settings)
    with context(lease, threading.Event()) as graph:
        graph.invoke({"incident_id": row["incident_id"], "request": PAYLOAD},
                     {"configurable": {"thread_id": row["thread_id"]}},
                     interrupt_before=["collect_evidence"])
    with connect() as connection:
        connection.execute("UPDATE checkpoints SET checkpoint=jsonb_set(checkpoint,'{v}',%s) WHERE thread_id=%s",
                           (Jsonb("corrupt"), row["thread_id"]))
    expire(connect, row["run_id"])
    Worker(repo, context, short_settings()).run(once=True)
    result = repo.latest(row["incident_id"])
    assert result["status"] == "failed" and result["last_error"]["code"] == "CHECKPOINT_UNREADABLE"
    assert counts(connect) == {}
