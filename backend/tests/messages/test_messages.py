"""4A-1 ECS checks: real transactions, API boundaries and a fresh process."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import subprocess
import sys
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.app.api.routes.messages import get_message_repository
from backend.app.config import ApiSettings
from backend.app.main import create_app
from backend.app.persistence.incidents import NewIncidentRecord, PostgresIncidentRepository
from backend.app.persistence.messages import PostgresMessageRepository, MessageError
from backend.app.persistence.migrations import run_migrations
from backend.app.services.message_schemas import MessageDraft
from backend.tests.runtime.test_worker_postgres import storage, accept


def legacy(connect, phase="remediation_skipped"):
    key = str(uuid4())
    PostgresIncidentRepository(connect).create(NewIncidentRecord(
        incident_id=key, thread_id=key, namespace="agent-demo", service_name="order-service",
        description="historical incident", phase=phase))
    return key


def client_for(repo, mode="queued"):
    app = create_app(ApiSettings(_env_file=None, environment="test", execution_mode=mode))
    app.dependency_overrides[get_message_repository] = lambda: repo
    # No incident service/graph is initialized; any accidental invocation fails.
    return TestClient(app)


def test_4A1_concurrency_and_conflicting_retries(storage):
    connect, _, _ = storage
    incident = legacy(connect)
    repo = PostgresMessageRepository(connect)
    draft = MessageDraft(client_message_id="same", content="  new information\n")
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda _: repo.append(incident, draft), range(16)))
    assert sum(r.created for r in receipts) == 1
    assert len({r.message.message_id for r in receipts}) == 1
    assert receipts[0].message.content == draft.content
    with pytest.raises(MessageError, match="MESSAGE_ID_CONFLICT"):
        repo.append(incident, draft.model_copy(update={"content": "different"}))
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda i: repo.append(incident, MessageDraft(
            client_message_id=f"unique-{i}", content=str(i))), range(12)))
    assert sorted(r.message.sequence for r in receipts) == list(range(2, 14))
    page = repo.list(incident, limit=5)
    assert [m.sequence for m in page.items] == [13, 12, 11, 10, 9]
    repo.append(incident, MessageDraft(client_message_id="late", content="late"))
    second = repo.list(incident, limit=5, before_sequence=page.next_before_sequence)
    third = repo.list(incident, limit=5, before_sequence=second.next_before_sequence)
    assert [m.sequence for m in second.items + third.items] == list(range(8, 0, -1))
    assert third.next_before_sequence is None
    def conflicting_write(content):
        try:
            return repo.append(incident, MessageDraft(client_message_id="race", content=content)).created
        except MessageError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(conflicting_write, ["version one", "version two"]))
    assert results.count(True) == 1 and results.count("MESSAGE_ID_CONFLICT") == 1
    other = legacy(connect)
    assert repo.append(other, draft).message.sequence == 1
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM incident_agent_app.runs").fetchone()["n"] == 0
        assert conn.execute("SELECT phase FROM incident_agent_app.incidents WHERE incident_id=%s",
                            (incident,)).fetchone()["phase"] == "remediation_skipped"


def test_4A1_api_validation_and_no_workflow(storage):
    connect, _, _ = storage
    incident = legacy(connect)
    url = f"/api/v1/incidents/{incident}/messages"
    repo = PostgresMessageRepository(connect)
    body = {"client_message_id": "browser-1", "content": "I restarted it; please approve"}
    with client_for(repo) as client:
        assert client.get(url).json() == {"items": [], "next_before_sequence": None}
        first = client.post(url, json=body)
        assert first.status_code == 201
        assert first.json()["processing"] == "not_started"
        assert first.json()["message"]["source"] == "user_supplied"
        assert client.post(url, json=body).status_code == 200
        assert client.post(url, json={**body, "content": "changed"}).status_code == 409
        for extra in ({"role": "assistant"}, {"source": "tool_observed"},
                      {"related_run_id": "other"}, {"evidence_refs": ["fake"]}):
            assert client.post(url, json={**body, **extra}).status_code == 422
        for content in (" ", "\x00", "x" * 16001):
            rejected = client.post(url, json={**body, "content": content})
            assert rejected.status_code == 422
            detail = rejected.json()["error"]
            assert detail["code"] == "REQUEST_VALIDATION_ERROR"
            assert detail["details"][0]["loc"] == ["body", "content"]
            assert set(detail["details"][0]) == {"loc", "type", "msg"}
        for query in ("?limit=0", "?limit=51", "?before_sequence=0", "?before_sequence=99999999999999999999"):
            assert client.get(url + query).status_code == 422
        assert client.get("/api/v1/incidents/missing/messages").status_code == 404
        assert client.post("/api/v1/incidents/missing/messages", json=body).status_code == 404
    with client_for(repo, "sync") as client:
        assert client.post(url, json=body).status_code == 409
        assert client.get(url).status_code == 200


def test_4A1_live_workflow_guard_and_internal_reply_dedup(storage):
    connect, runs, _ = storage
    accept(runs)
    with connect() as conn:
        run = conn.execute("SELECT * FROM incident_agent_app.runs").fetchone()
    repo = PostgresMessageRepository(connect)
    draft = MessageDraft(client_message_id="active", content="new facts")
    for status in ("queued", "running", "waiting_user", "waiting_approval", "retry_scheduled", "reconciling"):
        with connect() as conn:
            conn.execute("UPDATE incident_agent_app.runs SET status=%s WHERE run_id=%s", (status, run["run_id"]))
        with pytest.raises(MessageError, match="MESSAGE_WORKFLOW_BUSY"):
            repo.append(run["incident_id"], draft)
    pending = legacy(connect, "awaiting_approval")
    with pytest.raises(MessageError, match="MESSAGE_WORKFLOW_BUSY"):
        repo.append(pending, draft)
    with client_for(repo) as client:
        assert client.post(f"/api/v1/incidents/{pending}/messages",
                           json=draft.model_dump(include={"client_message_id", "content"})).status_code == 409
    assistant = MessageDraft(client_message_id="reply-step-1", content="saved report",
                             role="assistant", related_run_id=run["run_id"], evidence_refs=["e1"])
    assert repo.append(run["incident_id"], assistant).created
    assert not repo.append(run["incident_id"], assistant).created
    with pytest.raises(MessageError, match="MESSAGE_ID_CONFLICT"):
        repo.append(run["incident_id"], assistant.model_copy(update={"evidence_refs": ["e2"]}))
    with pytest.raises(MessageError, match="MESSAGE_RUN_MISMATCH"):
        repo.append(legacy(connect), assistant)


def test_4A1_storage_failure_never_acknowledged(storage):
    connect, _, _ = storage
    incident = legacy(connect)

    @contextmanager
    def failed_commit():
        with connect() as conn:
            yield conn
            raise psycopg.OperationalError("simulated commit failure: private DSN")

    body = {"client_message_id": "retryable", "content": "notes"}
    with client_for(PostgresMessageRepository(failed_commit)) as client:
        response = client.post(f"/api/v1/incidents/{incident}/messages", json=body)
        assert response.status_code == 503
        assert "private DSN" not in response.text
        assert client.get(f"/api/v1/incidents/{incident}/messages").status_code == 503
    repo = PostgresMessageRepository(connect)
    assert repo.list(incident).items == []
    assert repo.append(incident, MessageDraft(**body)).created


def test_4A1_fresh_process_reads_history_and_migration_is_repeatable(storage):
    connect, _, settings = storage
    incident = legacy(connect)
    repo = PostgresMessageRepository(connect)
    saved = repo.append(incident, MessageDraft(client_message_id="persist", content="survives restart"))
    with connect() as conn:
        assert run_migrations(conn) == []
    code = """
import os
import psycopg
from psycopg.rows import dict_row
from backend.app.persistence.messages import PostgresMessageRepository
def connect():
    return psycopg.connect(os.environ['MESSAGE_TEST_DSN'], row_factory=dict_row)
print(PostgresMessageRepository(connect).list(os.environ['MESSAGE_TEST_ID']).model_dump_json())
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
        timeout=30, env=dict(os.environ, MESSAGE_TEST_DSN=settings.database_url.get_secret_value(),
                            MESSAGE_TEST_ID=incident))
    assert result.returncode == 0, "fresh-process message read failed"
    assert json.loads(result.stdout)["items"][0]["message_id"] == saved.message.message_id
