"""Every test creates and retains its own guarded test database, never the demo DB."""
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from backend.app.agent.state import IncidentState
from backend.app.persistence.checkpointer import postgres_checkpointer
from backend.app.persistence.leases import LeaseLost, LeaseRepository
from backend.app.persistence.incidents import PostgresIncidentRepository
from backend.app.persistence.migrations import run_migrations
from backend.app.persistence.settings import DatabaseSettings
from backend.app.runtime.checkpointer import fenced_checkpointer
from backend.app.runtime.settings import WorkerSettings
from backend.app.runtime.worker import Worker, OwnedDependency
from backend.tests.run_1a_acceptance import isolated_database_url
from backend.app.services.incident_service import IncidentApplicationService
from backend.tests.api.fakes import FakeIncidentGraph

PAYLOAD = {"namespace": "default", "service_name": "demo", "description": "worker acceptance"}


@pytest.fixture
def storage():
    source = os.environ.get("INCIDENT_AGENT_TEST_DATABASE_URL")
    if not source:
        pytest.skip("requires isolated ECS PostgreSQL acceptance")
    assert conninfo_to_dict(source)["dbname"].startswith("incident_agent_test_")
    name = "incident_agent_test_1b_" + uuid4().hex
    with psycopg.connect(source, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = isolated_database_url(source, name)
    settings = DatabaseSettings(database_url=dsn)
    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)
    with connect() as connection:
        run_migrations(connection)
    with postgres_checkpointer(settings):
        pass
    return connect, LeaseRepository(connect), settings


def accept(repo):
    return repo.accept(incident_id=str(uuid4()), run_id=str(uuid4()), thread_id=str(uuid4()), payload=PAYLOAD, key=None)


def short_settings(**kwargs):
    return WorkerSettings(_env_file=None, heartbeat_seconds=0.2, lease_seconds=1,
                          poll_seconds=0.1, shutdown_seconds=1, **kwargs)


def graph_context(storage, *, delay=0, entered=None):
    _, repo, settings = storage
    @contextmanager
    def context(lease, lost):
        with fenced_checkpointer(settings, repo, lease, lost) as saver:
            builder = StateGraph(IncidentState)
            def work(state):
                if entered is not None:
                    entered.set()
                time.sleep(delay)
                return {"phase": "remediation_skipped"}
            builder.add_node("work", work)
            builder.add_edge(START, "work")
            builder.add_edge("work", END)
            yield builder.compile(checkpointer=saver)
    return context


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for worker condition")


def expire(connect, run_id):
    with connect() as connection:
        connection.execute("UPDATE incident_agent_app.runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE run_id=%s", (run_id,))


def test_two_claimers_and_stale_owner_cannot_publish(storage):
    connect, repo, _ = storage
    row = accept(repo)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda n: repo.claim(str(n), 30), range(8)))
    claimed = [claim for claim in claims if claim]
    assert len(claimed) == 1 and claimed[0]["attempt"] == 1
    old = claimed[0]
    expire(connect, row["run_id"])
    new = repo.claim("replacement", 30)
    assert new["lease_epoch"] == old["lease_epoch"] + 1
    for operation in (lambda: repo.heartbeat(old, 30), lambda: repo.finish(old, "succeeded")):
        with pytest.raises(LeaseLost):
            operation()
    class Tool:
        def execute(self):
            pytest.fail("stale worker dispatched a tool")
    with pytest.raises(LeaseLost):
        OwnedDependency(Tool(), repo, old, threading.Event()).execute()
    repo.finish(new, "succeeded")
    assert repo.latest(row["incident_id"])["status"] == "succeeded"


def test_api_can_read_first_claim_before_checkpoint_and_worker_presence(storage):
    connect, repo, _ = storage
    row = accept(repo)
    repo.announce("worker", 30)
    lease = repo.claim("worker", 30)
    service = IncidentApplicationService(FakeIncidentGraph(), PostgresIncidentRepository(connect),
                                         runs=repo, execution_mode="queued")
    snapshot = service.get_incident(row["incident_id"])
    assert snapshot.run["status"] == "running" and snapshot.worker_available
    assert snapshot.state["request"] == PAYLOAD
    repo.finish(lease, "failed", error_code="WORKER_FAILED")
    repo.withdraw("worker")
    snapshot = service.get_incident(row["incident_id"])
    assert snapshot.phase == "failed" and not snapshot.worker_available


def test_heartbeat_survives_long_synchronous_node(storage):
    _, repo, _ = storage
    row, entered = accept(repo), threading.Event()
    worker = Worker(repo, graph_context(storage, delay=2.2, entered=entered), short_settings())
    thread = threading.Thread(target=worker.run, kwargs={"once": True})
    thread.start()
    try:
        assert entered.wait(10)
        time.sleep(1.3)  # Longer than the initial one-second lease.
        assert repo.claim("competitor", 30) is None
        assert repo.worker_available()
    finally:
        thread.join(timeout=15)
    assert not thread.is_alive()
    result = repo.latest(row["incident_id"])
    assert result["status"] == "succeeded" and result["attempt"] == 1
    assert not repo.worker_available()


def test_checkpoint_and_pending_writes_are_fenced(storage):
    connect, repo, settings = storage
    row = accept(repo)
    old = repo.claim("old", 30)
    with graph_context(storage)(old, threading.Event()) as graph:
        graph.invoke({"incident_id": row["incident_id"], "request": PAYLOAD},
                     {"configurable": {"thread_id": row["thread_id"]}})
    expire(connect, row["run_id"])
    new = repo.claim("new", 30)
    with fenced_checkpointer(settings, repo, old, threading.Event()) as saver:
        saved = saver.get_tuple({"configurable": {"thread_id": row["thread_id"]}})
        with pytest.raises(LeaseLost):
            saver.put(saved.config, saved.checkpoint, saved.metadata, {})
        with pytest.raises(LeaseLost):
            saver.put_writes(saved.config, [("phase", "stale")], "old-task")
    repo.finish(new, "succeeded")


def test_attempt_budget_and_retry_not_before_due_time(storage):
    connect, repo, _ = storage
    row = accept(repo)
    first = repo.claim("one", 30)
    repo.finish(first, "retry_scheduled", retry_seconds=5, error_code="DEPENDENCY_TEMPORARY")
    assert repo.claim("early", 30) is None
    with connect() as connection:
        connection.execute("UPDATE incident_agent_app.runs SET next_retry_at=clock_timestamp()-interval '1 second' WHERE run_id=%s", (row["run_id"],))
    second = repo.claim("two", 30)
    assert second["attempt"] == 2
    expire(connect, row["run_id"])
    third = repo.claim("three", 30)
    assert third["attempt"] == 3
    expire(connect, row["run_id"])
    assert repo.claim("four", 30) is None
    result = repo.latest(row["incident_id"])
    assert result["status"] == "failed" and result["last_error"]["code"] == "ATTEMPTS_EXHAUSTED"


def test_saved_pending_checkpoint_is_not_blindly_replayed(storage):
    _, repo, _ = storage
    row = accept(repo)
    lease = repo.claim("seed", 30)
    with graph_context(storage)(lease, threading.Event()) as graph:
        # Persist input and pause before work; no dynamic approval interrupt exists.
        graph.invoke({"incident_id": row["incident_id"], "request": PAYLOAD},
                     {"configurable": {"thread_id": row["thread_id"]}},
                     interrupt_before=["work"])
    expire(storage[0], row["run_id"])
    worker = Worker(repo, graph_context(storage), short_settings())
    worker.run(once=True)
    result = repo.latest(row["incident_id"])
    assert result["status"] == "failed"
    assert result["last_error"]["code"] == "CHECKPOINT_REQUIRES_REVIEW"


def test_killed_process_lease_expires_and_task_is_found(storage):
    _, repo, settings = storage
    row = accept(repo)
    environment = dict(os.environ, PGVECTOR_URL=settings.database_url.get_secret_value())
    code = """
import time
from contextlib import contextmanager
from functools import partial
from backend.app.persistence.database import connect_database
from backend.app.persistence.settings import get_database_settings
from backend.app.persistence.leases import LeaseRepository
from backend.app.runtime.worker import Worker
from backend.app.runtime.settings import WorkerSettings
repo=LeaseRepository(partial(connect_database,get_database_settings()))
@contextmanager
def blocked(lease,lost):
    time.sleep(60)
    yield None
Worker(repo,blocked,WorkerSettings(_env_file=None,lease_seconds=1,heartbeat_seconds=0.2)).run(once=True)
"""
    process = subprocess.Popen([sys.executable, "-c", code], env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_until(lambda: repo.latest(row["incident_id"])["status"] == "running")
        process.kill()
        process.wait(timeout=10)
        time.sleep(1.3)
        worker = Worker(repo, graph_context(storage), short_settings())
        worker.run(once=True)
        result = repo.latest(row["incident_id"])
        assert result["status"] == "succeeded" and result["attempt"] == 2
        assert result["run_id"] == row["run_id"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_actual_interrupt_remains_waiting_and_is_not_claimed(storage):
    _, repo, settings = storage
    row = accept(repo)
    @contextmanager
    def context(lease, lost):
        with fenced_checkpointer(settings, repo, lease, lost) as saver:
            builder = StateGraph(IncidentState)
            builder.add_node("prepare", lambda state: {"phase": "awaiting_approval", "approval_status": "pending"})
            def wait(state):
                interrupt({"kind": "approval"})
                pytest.fail("worker must not resume approval")
            builder.add_node("wait", wait)
            builder.add_edge(START, "prepare")
            builder.add_edge("prepare", "wait")
            builder.add_edge("wait", END)
            yield builder.compile(checkpointer=saver)
    Worker(repo, context, short_settings()).run(once=True)
    assert repo.latest(row["incident_id"])["status"] == "waiting_approval"
    assert repo.claim("another", 30) is None


def test_shutdown_stops_renewing_without_releasing_live_work(storage):
    _, repo, _ = storage
    row, entered = accept(repo), threading.Event()
    worker = Worker(repo, graph_context(storage, delay=3, entered=entered), short_settings())
    thread = threading.Thread(target=worker.run, kwargs={"once": True})
    thread.start()
    assert entered.wait(10)
    worker.stop.set()
    thread.join(timeout=4)
    assert not thread.is_alive()
    assert worker.lost.is_set()
    result = repo.latest(row["incident_id"])
    assert result["status"] == "running"  # Not falsely completed/released on shutdown.
    time.sleep(3.2)  # Let the old node return and attempt its now-forbidden checkpoint write.
    assert repo.latest(row["incident_id"])["status"] == "running"
    assert repo.claim("replacement", 30)["lease_epoch"] == 2
