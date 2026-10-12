"""1A acceptance against an explicitly selected, isolated PostgreSQL database.

No destructive cleanup: each test uses new UUIDs. Never defaults to PGVECTOR_URL.
"""
import os
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Lock
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from backend.app.agent.schemas import IncidentRequest
from backend.app.api.dependencies import get_incident_service
from backend.app.api.dependencies import build_incident_service
from backend.app.config import ApiSettings
from backend.app.main import create_app
from backend.app.persistence.incidents import NewIncidentRecord, PostgresIncidentRepository
from backend.app.persistence.migrations import run_migrations
from backend.app.persistence.checkpointer import postgres_checkpointer
from backend.app.persistence.settings import DatabaseSettings
from backend.app.persistence.runs import PostgresRunRepository, RunError
from backend.app.services.incident_service import IncidentApplicationService, IncidentGraphError
from backend.tests.api.fakes import FakeIncidentGraph

PAYLOAD = {"namespace": "default", "service_name": "demo", "description": "1A durable acceptance"}


@pytest.fixture(scope="module")
def connect():
    dsn = os.environ.get("INCIDENT_AGENT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set INCIDENT_AGENT_TEST_DATABASE_URL to run real PostgreSQL acceptance")
    assert conninfo_to_dict(dsn).get("dbname", "").startswith("incident_agent_test_"), "Dedicated test database required"
    # functools.partial repr exposes its DSN in pytest's fixture traceback.
    def factory():
        return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)
    with factory() as connection:
        run_migrations(connection)
    return factory


def service_for(connect, graph=None):
    return IncidentApplicationService(graph or FakeIncidentGraph(), PostgresIncidentRepository(connect),
                                      runs=PostgresRunRepository(connect), execution_mode="queued")


@contextmanager
def client_for(service):
    app = create_app(ApiSettings(environment="test"))
    app.dependency_overrides[get_incident_service] = lambda: service
    with TestClient(app) as client:
        yield client


def accept(repo, key=None, **overrides):
    values = dict(incident_id=str(uuid4()), run_id=str(uuid4()), thread_id=str(uuid4()), payload=PAYLOAD, key=key)
    values.update(overrides)
    return repo.accept(**values)


def test_concurrent_same_key_is_one_committed_incident_and_run(connect):
    # Force every caller past its initial lookup before anyone inserts, exercising
    # the unique-constraint loser/rollback path instead of only the fast replay path.
    barrier, lock = Barrier(8), Lock()
    class RacingRepository(PostgresRunRepository):
        reads = 0
        def by_key(self, key):
            row = super().by_key(key)
            with lock:
                self.reads += 1
                first_wave = self.reads <= 8
            if first_wave:
                barrier.wait(timeout=30)
            return row
    repo, key = RacingRepository(connect), "concurrent-" + str(uuid4())
    with connect() as connection:
        before = connection.execute("SELECT count(*) AS n FROM incident_agent_app.incidents").fetchone()["n"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _: accept(repo, key), range(8)))
    assert len({row["run_id"] for row in rows}) == 1
    assert len({row["incident_id"] for row in rows}) == 1
    with connect() as connection:
        assert connection.execute("SELECT count(*) AS n FROM incident_agent_app.incidents").fetchone()["n"] == before + 1
        assert connection.execute("SELECT count(*) AS n FROM incident_agent_app.runs WHERE idempotency_key=%s", (key,)).fetchone()["n"] == 1


def test_http_replay_conflict_find_and_reconstructed_service(connect):
    graph, key = FakeIncidentGraph(), "http-" + str(uuid4())
    with client_for(service_for(connect, graph)) as client:
        first = client.post("/api/v1/incidents", json=PAYLOAD, headers={"Idempotency-Key": key})
        assert first.status_code == 202, first.text
        body = first.json()
        assert body["run"]["status"] == "queued" and body["run"]["attempt"] == 0
        assert body["phase"] == "created" and body["worker_available"] is False
        assert body["execution_mode"] == "queued" and not body["waiting_for_approval"]
        assert body["thread_id"] != body["incident_id"]
        replay = client.post("/api/v1/incidents", json={**PAYLOAD, "description": " " + PAYLOAD["description"] + " "}, headers={"Idempotency-Key": key})
        assert replay.status_code == 202 and replay.json()["run"] == body["run"]
        conflict = client.post("/api/v1/incidents", json={**PAYLOAD, "description": "different"}, headers={"Idempotency-Key": key})
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert client.get("/api/v1/incidents/by-idempotency-key/missing-" + str(uuid4())).status_code == 404
        assert client.post("/api/v1/incidents", json=PAYLOAD, headers={"Idempotency-Key": "bad key"}).status_code == 422
        first_without_key = client.post("/api/v1/incidents", json=PAYLOAD).json()
        second_without_key = client.post("/api/v1/incidents", json=PAYLOAD).json()
        assert first_without_key["incident_id"] != second_without_key["incident_id"]
    assert not graph.invocations and not graph.state_reads
    with client_for(service_for(connect)) as restarted:
        found = restarted.get("/api/v1/incidents/by-idempotency-key/" + key)
        assert found.status_code == 200 and found.json() == body
        assert restarted.get("/api/v1/incidents/" + body["incident_id"]).json() == body


def test_second_insert_failure_rolls_back_incident_and_returns_503(connect):
    repo = PostgresRunRepository(connect)
    existing = accept(repo)
    new_incident = str(uuid4())
    # Duplicate run PK fails AFTER a valid incident insert with a different thread.
    with pytest.raises(RunError):
        accept(repo, incident_id=new_incident, run_id=existing["run_id"])
    assert PostgresIncidentRepository(connect).get(new_incident) is None
    class FailingRepository(PostgresRunRepository):
        def accept(self, **kwargs):
            return super().accept(**{**kwargs, "run_id": existing["run_id"]})
    service = IncidentApplicationService(FakeIncidentGraph(), PostgresIncidentRepository(connect),
        runs=FailingRepository(connect), execution_mode="queued")
    with client_for(service) as client:
        response = client.post("/api/v1/incidents", json=PAYLOAD)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "RUN_STORAGE_UNAVAILABLE"


def test_metadata_keyset_pagination_and_scope(connect):
    repo = PostgresRunRepository(connect)
    for _ in range(3):
        accept(repo)
    with client_for(service_for(connect)) as client:
        ids, cursor = [], None
        while True:
            params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
            response = client.get("/api/v1/incidents", params=params)
            assert response.status_code == 200
            page = response.json()
            for item in page["items"]:
                assert "description" not in item and "input_payload" not in item
                assert "evidence" not in item and "idempotency_key" not in item
                ids.append(item["incident_id"])
            cursor = page["next_cursor"]
            if not cursor:
                break
            assert client.get(f"/api/v1/incidents/{ids[0]}/runs", params={"cursor": cursor}).status_code == 422
        assert len(ids) == len(set(ids))
        runs = client.get(f"/api/v1/incidents/{ids[0]}/runs").json()
        assert len(runs["items"]) <= 1  # Historical rows may have no runs.
        assert client.get("/api/v1/incidents", params={"limit": 51}).status_code == 422
        assert client.get("/api/v1/incidents", params={"cursor": "garbage"}).status_code == 422


def test_old_mapping_read_and_executed_missing_checkpoint_is_error(connect):
    graph = FakeIncidentGraph()
    incident_id, thread_id = str(uuid4()), "legacy-" + str(uuid4())
    PostgresIncidentRepository(connect).create(NewIncidentRecord(incident_id=incident_id, thread_id=thread_id, **PAYLOAD))
    graph.states[thread_id] = {"incident_id": incident_id, "request": PAYLOAD, "phase": "completed"}
    service = service_for(connect, graph)
    old = service.get_incident(incident_id)
    assert old.run is None and old.thread_id == thread_id
    row = accept(PostgresRunRepository(connect))
    with connect() as connection:
        connection.execute("UPDATE incident_agent_app.runs SET status='failed',attempt=1 WHERE run_id=%s", (row["run_id"],))
    with pytest.raises(IncidentGraphError):
        service.get_incident(row["incident_id"])
    assert not graph.invocations
    assert PostgresRunRepository(connect).latest(row["incident_id"])["status"] == "failed"
    with connect() as connection:
        assert run_migrations(connection) == []


def test_real_postgres_checkpoint_legacy_read_and_finished_key_replay(connect):
    settings = DatabaseSettings(database_url=os.environ["INCIDENT_AGENT_TEST_DATABASE_URL"])
    repository, runs = PostgresIncidentRepository(connect), PostgresRunRepository(connect)
    legacy_id, legacy_thread = str(uuid4()), "old-thread-" + str(uuid4())
    repository.create(NewIncidentRecord(incident_id=legacy_id, thread_id=legacy_thread, **PAYLOAD))
    key = "finished-" + str(uuid4())
    row = accept(runs, key)
    with postgres_checkpointer(settings) as saver:
        service = build_incident_service(checkpointer=saver, repository=repository, runs=runs, execution_mode="queued")
        # Write synthetic historical state through LangGraph's checkpoint API;
        # no node is invoked and no Kubernetes/model dependency is constructed.
        graph = service._graph._graph
        for incident_id, thread_id in ((legacy_id, legacy_thread), (row["incident_id"], row["thread_id"])):
            graph.update_state({"configurable": {"thread_id": thread_id}},
                               {"incident_id": incident_id, "request": PAYLOAD, "phase": "completed"},
                               as_node="finish_failure")
    with connect() as connection:
        connection.execute("UPDATE incident_agent_app.runs SET status='succeeded',attempt=1,finished_at=now() WHERE run_id=%s", (row["run_id"],))
    with postgres_checkpointer(settings) as saver:
        restarted = build_incident_service(checkpointer=saver, repository=repository, runs=runs, execution_mode="queued")
        with client_for(restarted) as client:
            old = client.get("/api/v1/incidents/" + legacy_id)
            assert old.status_code == 200 and old.json()["run"] is None
            assert old.json()["thread_id"] == legacy_thread
            replay = client.post("/api/v1/incidents", json=PAYLOAD, headers={"Idempotency-Key": key})
            assert replay.status_code == 202, replay.text
            assert replay.json()["run"]["run_id"] == row["run_id"]
            assert replay.json()["run"]["status"] == "succeeded"


def test_fresh_api_process_finds_committed_task(connect):
    environment = dict(os.environ)
    environment["PGVECTOR_URL"] = environment["INCIDENT_AGENT_TEST_DATABASE_URL"]
    environment["ACCEPTANCE_KEY"] = "process-" + str(uuid4())
    common = """
import json, os
from backend.app.api.dependencies import incident_service_context
from backend.app.agent.schemas import IncidentRequest
with incident_service_context(execution_mode='queued') as service:
"""
    create = common + """
    snapshot = service.create_incident(IncidentRequest(namespace='default',service_name='demo',description='process test'),idempotency_key=os.environ['ACCEPTANCE_KEY'])
    print(json.dumps([snapshot.incident_id,snapshot.run['run_id'],snapshot.run['status']]))
"""
    read = common + """
    snapshot = service.get_by_idempotency_key(os.environ['ACCEPTANCE_KEY'])
    print(json.dumps([snapshot.incident_id,snapshot.run['run_id'],snapshot.run['status']]))
"""
    first = subprocess.run([sys.executable, "-c", create], env=environment, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stderr
    second = subprocess.run([sys.executable, "-c", read], env=environment, capture_output=True, text=True, timeout=60)
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout) == json.loads(second.stdout)
    assert json.loads(second.stdout)[2] == "queued"


def test_active_run_constraint_and_run_pagination_with_equal_timestamps(connect):
    repo = PostgresRunRepository(connect)
    row = accept(repo)
    insert = """INSERT INTO incident_agent_app.runs
        (run_id,incident_id,thread_id,input_payload,input_sha256,request_sha256,status)
        SELECT %s,incident_id,%s,input_payload,input_sha256,request_sha256,%s
        FROM incident_agent_app.runs WHERE run_id=%s"""
    with pytest.raises(psycopg.errors.UniqueViolation):
        with connect() as connection:
            connection.execute(insert, (str(uuid4()), str(uuid4()), "queued", row["run_id"]))
    with connect() as connection:
        # All created_at values in this transaction are equal; IDs break ties.
        for _ in range(4):
            connection.execute(insert, (str(uuid4()), str(uuid4()), "failed", row["run_id"]))
    cursor, ids = None, []
    while True:
        page = repo.list_metadata(incident_id=row["incident_id"], limit=2, cursor=cursor)
        ids.extend(item["run_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(ids) == len(set(ids)) == 5
