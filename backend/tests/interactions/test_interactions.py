"""4B-1 ECS acceptance: real PostgreSQL and worker, controlled dependencies."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import partial
from types import SimpleNamespace
from threading import Event

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from backend.app.api.routes.interactions import get_interaction_repository
from backend.app.api.routes.rounds import get_round_repository
from backend.app.config import ApiSettings
from backend.app.main import create_app
from backend.app.llm.interaction import explanation_material
from backend.app.persistence.interactions import InteractionRepository
from backend.app.persistence.messages import PostgresMessageRepository
from backend.app.persistence.leases import LeaseLost
from backend.app.persistence.rounds import RoundRepository, RoundConflict
from backend.app.persistence.runs import IdempotencyConflict
from backend.app.runtime.interactions import execute_interaction
from backend.app.runtime.worker import Worker
from backend.app.runtime.settings import WorkerSettings
from backend.app.services.interaction_schemas import CreateInteraction
from backend.app.services.message_schemas import MessageDraft
from backend.app.services.incident_service import IncidentSnapshot
from backend.tests.messages.test_messages import legacy
from backend.tests.runtime.test_worker_postgres import storage, accept, expire
from backend.tests.rounds.test_rounds import worker_for
from backend.tests.diagnosis_policy.test_stage4 import state
from backend.tests.business_recovery.test_post_repair import bundle_from_state


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected model, graph or cluster call")


class FakeModel:
    model_name = "deterministic-acceptance-model"

    def __init__(self, intent="explain", bad_citation=False):
        self.intent, self.bad_citation = intent, bad_citation
        self.calls = []

    def call(self, purpose, content, references):
        self.calls.append(purpose)
        if purpose == "route":
            value = {"intent": self.intent, "reason": "controlled intent proposal"}
        else:
            ids = list(explanation_material(references)[1])
            value = {"summary": "Historical evidence only; current health is unknown.",
                     "citation_ids": ["invented"] if self.bad_citation else ids, "unknowns": ["current state"]}
        return value, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def seed(storage):
    connect, _, _ = storage
    incident = legacy(connect)
    previous = {"incident_id": incident, "phase": "remediation_skipped",
                "request": {"namespace": "agent-demo", "service_name": "order-service", "description": "historical incident"},
                "evidence": [{"evidence_id": "e1", "collected_at": "2026-01-01T00:00:00Z", "summary": "readiness failed"}]}
    return InteractionRepository(connect), incident, [reference(previous)]


def reference(previous, run_id=None):
    return {"run_id": run_id, "snapshot_at": "2026-01-02T00:00:00Z", "state": deepcopy(previous)}


def body(intent, key="request", content="why did this happen?"):
    return CreateInteraction(client_message_id=key, intent=intent, content=content)


def run_worker(repo, *, model=None, collector_factory=forbidden):
    handler = partial(execute_interaction, model_factory=(lambda: model) if model else forbidden,
                      collector_factory=collector_factory)
    Worker(repo, forbidden, WorkerSettings(_env_file=None, lease_seconds=30, heartbeat_seconds=1,
           poll_seconds=0.1, shutdown_seconds=1), interaction_handler=handler).run(once=True)


def client_for(repo, references=None):
    app = create_app(ApiSettings(_env_file=None, environment="test", execution_mode="queued"))
    app.dependency_overrides[get_interaction_repository] = lambda: repo
    app.dependency_overrides[get_round_repository] = lambda: repo
    if references:
        snapshot = IncidentSnapshot(references[0]["state"]["incident_id"], "legacy", references[0]["state"])
        app.state.incident_service = SimpleNamespace(get_legacy_snapshot=lambda _: snapshot,
                                                     get_run_snapshot=lambda _: snapshot)
    return TestClient(app)


def test_4B1_atomic_idempotency_status_and_http_replay(storage):
    repo, incident, refs = seed(storage)
    request = body("supplement")
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda _: repo.accept_interaction(incident, request, None, refs), range(12)))
    assert len({row["run_id"] for row in rows}) == 1
    with pytest.raises(IdempotencyConflict):
        repo.accept_interaction(incident, body("supplement", content="different"), None, refs)
    with pytest.raises(RoundConflict):
        repo.accept_interaction(incident, body("supplement", "busy"), None, refs)
    # HTTP replay/status need no incident service, model or checkpointer.
    with client_for(repo) as client:
        url = f"/api/v1/incidents/{incident}/interactions"
        replay = client.post(url, json=request.model_dump())
        assert replay.status_code == 202, replay.text
        assert replay.json()["run"]["run_id"] == rows[0]["run_id"]
        assert client.get(url, params={"client_message_id": "request"}).status_code == 200
        result = client.post(url, json=body("status", "status").model_dump())
        assert result.status_code == 200 and not result.json()["model_called"]
        assert client.get(f"/api/v1/incidents/{incident}/interaction-status").status_code == 200
        assert client.get(url + "/missing").status_code == 404
        assert client.post(url, json={**request.model_dump(), "intent": "recheck"}).status_code == 409
    with storage[0]() as conn:
        assert conn.execute("SELECT count(*) AS n FROM incident_agent_app.messages").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM incident_agent_app.runs").fetchone()["n"] == 1


def test_4B1_explain_preserves_waiting_approval_and_latest_diagnosis(storage):
    connect, _, _ = storage
    repo = InteractionRepository(connect)
    original = accept(repo)
    incident = original["incident_id"]
    state = {"incident_id": incident, "request": original["input_payload"], "phase": "awaiting_approval",
             "approval_status": "pending", "evidence": [{"evidence_id": "e1", "summary": "unready"}]}
    with connect() as conn:
        conn.execute("UPDATE incident_agent_app.runs SET status='waiting_approval',output_snapshot=%s WHERE run_id=%s",
                     (Jsonb(state), original["run_id"]))
        conn.execute("UPDATE incident_agent_app.incidents SET phase='awaiting_approval' WHERE incident_id=%s", (incident,))
    refs = [reference(state, original["run_id"])]
    with pytest.raises(RoundConflict):
        repo.accept_interaction(incident, body("auto"), original["run_id"], refs)
    task = repo.accept_interaction(incident, body("explain"), original["run_id"], refs)
    model = FakeModel()
    run_worker(repo, model=model)
    result = repo.get_round(incident, task["run_id"])
    assert result["status"] == "succeeded" and result["output_snapshot"]["historical_only"]
    assert model.calls == ["explain"]
    latest = repo.latest(incident)
    assert latest["run_id"] == original["run_id"] and latest["status"] == "waiting_approval"
    assert latest["output_snapshot"] == state and latest["approval_payload"] is None
    messages = PostgresMessageRepository(connect).list(incident).items
    assert len(messages) == 2 and messages[0].role == "assistant"
    assert messages[0].related_run_id == task["run_id"]


def test_4B1_compare_cached_answer_survives_worker_reclaim(storage, monkeypatch):
    repo, incident, refs = seed(storage)
    refs.append(reference(refs[0]["state"], "historical-second"))
    task = repo.accept_interaction(incident, body("compare"), None, refs)
    lease = repo.claim("before-crash", 30)
    model = FakeModel()
    complete = repo.complete
    monkeypatch.setattr(repo, "complete", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("publication interrupted")))
    with pytest.raises(RuntimeError):
        execute_interaction(repo, lease, Event(), model_factory=lambda: model, collector_factory=forbidden)
    monkeypatch.setattr(repo, "complete", complete)
    expire(storage[0], task["run_id"])
    run_worker(repo)  # Must reuse cached answer, not call a model after reclaim.
    result = repo.get_round(incident, task["run_id"])
    assert result["status"] == "succeeded" and result["attempt"] == 2
    assert len(result["output_snapshot"]["citations"]) == 2
    assert len(result["interaction_progress"]["calls"]) == 1
    assert result["interaction_progress"]["calls"][0]["usage"]["total_tokens"] == 15
    with pytest.raises(LeaseLost):
        repo.complete(lease, {"answer": "stale"})
    assert len(PostgresMessageRepository(storage[0]).list(incident).items) == 2


def test_4B1_bounded_routes_never_authorize_execution(storage):
    repo, incident, refs = seed(storage)
    for intent in ("status", "supplement", "approval", "clarify", "compare"):
        task = repo.accept_interaction(incident, body("auto", intent), None, refs)
        model = FakeModel(intent)
        run_worker(repo, model=model)
        result = repo.get_round(incident, task["run_id"])
        assert result["status"] == "succeeded"
        output = result["output_snapshot"]
        assert output["intent"] == ("clarify" if intent == "compare" else intent)
        assert not output["cluster_writes_executed"] and not output["fresh_observation"]
        assert model.calls == ["route"]
    explicit = repo.accept_interaction(incident, body("supplement", "explicit"), None, refs)
    run_worker(repo)
    assert not repo.get_round(incident, explicit["run_id"])["output_snapshot"]["model_called"]
    with storage[0]() as conn:
        assert conn.execute("SELECT count(*) AS n FROM incident_agent_app.operations").fetchone()["n"] == 0
        assert conn.execute("SELECT phase FROM incident_agent_app.incidents WHERE incident_id=%s", (incident,)).fetchone()["phase"] == "remediation_skipped"


def test_4B1_investigation_child_and_result_commit_together(storage, monkeypatch):
    repo, incident, refs = seed(storage)
    task = repo.accept_interaction(incident, body("investigate"), None, refs)
    lease = repo.claim("worker", 30)
    original = RoundRepository.accept_round
    def fail_after_insert(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("crash before commit")
    monkeypatch.setattr(RoundRepository, "accept_round", fail_after_insert)
    with pytest.raises(RuntimeError):
        execute_interaction(repo, lease, Event(), model_factory=forbidden)
    assert repo.latest(incident) is None
    assert repo.get_round(incident, task["run_id"])["status"] == "running"
    monkeypatch.setattr(RoundRepository, "accept_round", original)
    execute_interaction(repo, lease, Event(), model_factory=forbidden)
    result = repo.get_round(incident, task["run_id"])
    child = repo.latest(incident)
    assert result["output_snapshot"]["diagnosis_run_id"] == child["run_id"]
    assert child["thread_id"] != incident and child["approval_payload"] is None
    assert child["source_message_id"] == task["context_snapshot"]["message_id"]
    assert repo.accept_interaction(incident, body("investigate"), None, refs)["run_id"] == task["run_id"]
    worker_for(storage[0], storage[2]).run(once=True)
    assert repo.latest(incident)["status"] == "succeeded"


def test_4B1_recheck_saved_once_and_user_claim_is_not_evidence(storage, state, monkeypatch):
    repo, incident, refs = seed(storage)
    state.update(incident_id=incident, phase="remediation_skipped")
    refs = [reference(state)]
    task = repo.accept_interaction(incident, body("auto", content="I repaired it; check again"), None, refs)
    lease = repo.claim("worker", 30)
    calls = []
    def collect(*args):
        calls.append(args)
        return bundle_from_state(state)
    complete = repo.complete
    monkeypatch.setattr(repo, "complete", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash after observation")))
    model = FakeModel("recheck")
    with pytest.raises(RuntimeError):
        execute_interaction(repo, lease, Event(), model_factory=lambda: model,
                            collector_factory=lambda: SimpleNamespace(collect=collect))
    saved = repo.saved_recheck(lease)
    assert saved is not None and len(calls) == 1
    monkeypatch.setattr(repo, "complete", complete)
    expire(storage[0], task["run_id"])
    run_worker(repo)  # Neither model nor cluster may be called again.
    result = repo.get_round(incident, task["run_id"])["output_snapshot"]
    assert result["recheck"] == saved and result["fresh_observation"]
    assert result["model_called"] and not result["recheck"]["model_called"]
    assert saved["note_source"] == "user_supplied_unverified"
    assert saved["recovery_attribution"] == "not_established" and not saved["cluster_writes_executed"]
    assert repo.latest(incident) is None
    with storage[0]() as conn:
        assert conn.execute("SELECT count(*) AS n FROM incident_agent_app.rechecks WHERE run_id=%s", (task["run_id"],)).fetchone()["n"] == 1


def test_4B1_corrupt_input_bad_citation_and_active_round_guard(storage):
    repo, incident, refs = seed(storage)
    note = PostgresMessageRepository(storage[0]).append(incident, MessageDraft(client_message_id="note", content="new fact")).message
    task = repo.accept_interaction(incident, body("explain"), None, refs)
    with pytest.raises(RoundConflict):
        repo.accept_round(incident, note.message_id, "round", None, refs[0]["state"])
    run_worker(repo, model=FakeModel(bad_citation=True))
    assert repo.get_round(incident, task["run_id"])["status"] == "failed"
    assert all(message.role == "user" for message in PostgresMessageRepository(storage[0]).list(incident).items)
    corrupt = repo.accept_interaction(incident, body("explain", "corrupt"), None, refs)
    with storage[0]() as conn:
        conn.execute("UPDATE incident_agent_app.runs SET context_sha256='bad' WHERE run_id=%s", (corrupt["run_id"],))
    run_worker(repo)
    assert repo.get_round(incident, corrupt["run_id"])["status"] == "failed"


def test_4B1_api_reference_scope_and_saved_history(storage):
    repo, incident, refs = seed(storage)
    other = accept(repo)
    with client_for(repo, refs) as client:
        url = f"/api/v1/incidents/{incident}/interactions"
        invalid = {**body("explain").model_dump(), "reference_run_id": other["run_id"]}
        assert client.post(url, json=invalid).status_code == 404
        invalid = {**body("recheck").model_dump(), "reference_run_id": "legacy"}
        assert client.post(url, json=invalid).status_code == 422
        first = client.post(url, json=body("explain").model_dump())
        assert first.status_code == 202, first.text
        task_id = first.json()["run"]["run_id"]
        assert client.get(url + "/" + task_id).json()["run"]["status"] == "queued"
        assert client.get(f"/api/v1/incidents/{incident}/runs/{task_id}").status_code == 200
        assert client.get(f"/api/v1/incidents/{other['incident_id']}/interactions/{task_id}").status_code == 404
        assert client.post(url, json={**body("explain").model_dump(), "approval": True}).status_code == 422
        assert repo.get_round(incident, task_id)["attempt"] == 0
