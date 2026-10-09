"""Actual API/worker/repository integration, controlled model/collector/Kubernetes."""
from contextlib import contextmanager
from copy import deepcopy
from functools import partial
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.config import ApiSettings
from backend.app.main import create_app
from backend.app.api.dependencies import build_incident_service
from backend.app.api.routes.controls import get_control_repository
from backend.app.persistence.checkpointer import postgres_checkpointer
from backend.app.persistence.controls import ControlRepository
from backend.app.persistence.incidents import PostgresIncidentRepository
from backend.app.persistence.operations import approval_binding, ApprovalConflict
from backend.app.persistence.rounds import RoundConflict
from backend.app.runtime.worker import Worker, production_graph
from backend.app.runtime.settings import WorkerSettings
from backend.app.services.control_schemas import AnswerQuestion, ControlRequest
from backend.app.services.round_context import INVESTIGATION_WORKFLOW, DIALOGUE_WORKFLOW
from backend.app.agent.schemas import RecoveryVerificationResult
from backend.app.investigation.production import deterministic_plan
from backend.app.tools.investigation import ReadOnlyToolbox
from backend.tests.runtime.test_worker_postgres import storage, expire
from backend.tests.runtime.test_operations_postgres import Kubernetes
from backend.tests.diagnosis_policy.test_stage4 import state, diagnosis, drift
from backend.tests.investigation.test_tools import identified, toolbox
from backend.tests.investigation_loop.test_loop import Model, stop, conclusion
from backend.tests.investigation_dialogue.test_dialogue import ask


@pytest.fixture
def system(storage, identified, toolbox, monkeypatch):
    import backend.app.config as config
    import backend.app.investigation.production as production
    import backend.app.tools.investigation as tools
    import backend.app.runtime.worker as runtime
    import backend.app.runtime.operations as operations
    repo = ControlRepository(storage[0])
    settings = ApiSettings(_env_file=None, environment="test", execution_mode="queued", investigation_enabled=True)
    monkeypatch.setattr(config, "get_api_settings", lambda: settings)
    model = Model(conclusion)
    def baseline(budget):
        return {**deepcopy(identified), "incident_id": budget.lease["incident_id"], "request": budget.lease["input_payload"],
                "round_context": budget.lease["context_snapshot"]}
    monkeypatch.setattr(production, "prepare_baseline", baseline)
    monkeypatch.setattr(production, "LazyModel", lambda: model)
    monkeypatch.setattr(tools, "build_investigation_toolbox", lambda budget, state: ReadOnlyToolbox(toolbox.clients, budget, state))
    kube = Kubernetes()
    monkeypatch.setattr(runtime, "create_clients", lambda **_: kube.clients)
    monkeypatch.setattr(operations, "revalidate_live_profile", lambda *_: {"resource_version": "10"})
    verifier = Mock()
    def verified(current):
        result = current["action_result"]
        execution_id = result.execution_id if hasattr(result, "execution_id") else result["execution_id"]
        return RecoveryVerificationResult(execution_id=execution_id, action="patch_service_selector", status="succeeded",
            started_at="2026-10-08T00:00:00Z", finished_at="2026-10-08T00:00:01Z", attempts=1, message="controlled verification",
            verification_scope="resources_and_registered_business", resource_status="ready", business_status="passed")
    verifier.verify.side_effect = verified
    monkeypatch.setattr(runtime, "build_recovery_verifier", lambda **_: verifier)
    # Any old diagnose/plan model factory on a new workflow is a regression.
    monkeypatch.setattr(runtime, "build_diagnosis_service", lambda: pytest.fail("old diagnosis LLM constructed"))
    monkeypatch.setattr(runtime, "build_remediation_planner", lambda: pytest.fail("extra planner LLM constructed"))
    def worker():
        return Worker(repo, partial(production_graph, storage[2], repo), WorkerSettings(_env_file=None,
            lease_seconds=60, heartbeat_seconds=1, poll_seconds=0.1, shutdown_seconds=1))
    def work():
        worker().run(once=True)
    @contextmanager
    def client():
        with postgres_checkpointer(storage[2]) as saver:
            service = build_incident_service(checkpointer=saver, repository=PostgresIncidentRepository(storage[0]),
                                             runs=repo, execution_mode="queued")
            app = create_app(settings)
            app.state.incident_service = service
            app.dependency_overrides[get_control_repository] = lambda: repo
            with TestClient(app) as http:
                yield http, service
    def create():
        return repo.accept(incident_id=str(uuid4()), run_id=str(uuid4()), thread_id=str(uuid4()),
                           payload=identified["request"], key=None, dialogue=True)
    return SimpleNamespace(repo=repo, state=identified, model=model, kube=kube, verifier=verifier,
                           settings=settings, work=work, worker=worker, client=client, create=create, storage=storage)


def snapshot(system, row):
    with system.client() as (_, service):
        return service.get_run_snapshot(system.repo.get_round(row["incident_id"], row["run_id"])).state


def setup_drift(system, action="patch_service_selector"):
    drift(system.state, action)
    parsed = deepcopy(system.state.pop("diagnosis"))
    for key in ("remediation_plan", "requires_approval"):
        system.state.pop(key, None)
    system.kube.resource.spec.selector = deepcopy(system.state["evidence"][0]["data"]["selector"])
    system.model.choose = lambda _: {"action": "propose_plan", "candidate": action, "diagnosis": parsed}


def approve(system, row, saved, approved=True, *, via_api=False):
    request = saved["approval_request"]
    approval_id = request.approval_id if hasattr(request, "approval_id") else request["approval_id"]
    decision = {"approval_id": approval_id, "approved": approved, "approver": "ecs-acceptance", "comment": "controlled test"}
    if via_api:
        with system.client() as (http, _):
            response = http.post(f"/api/v1/incidents/{row['incident_id']}/approval", json=decision)
            assert response.status_code == 200, response.text
    else:
        system.repo.queue_approval(row["run_id"], decision, approval_binding(saved))
    return decision


def test_api_create_and_read_runs_real_worker_without_old_planner(system):
    with system.client() as (http, service):
        response = http.post("/api/v1/incidents", json=system.state["request"], headers={"Idempotency-Key": "new-workflow"})
        assert response.status_code == 202, response.text
        incident = response.json()["incident_id"]
        row = system.repo.latest(incident)
        assert row["workflow_version"] == INVESTIGATION_WORKFLOW and not system.model.prompts
        system.work()
        displayed = http.get("/api/v1/incidents/" + incident)
        assert displayed.status_code == 200, displayed.text
        assert displayed.json()["diagnosis"]["fault_category"] == "no_fault_detected"
        assert system.repo.latest(incident)["status"] == "succeeded" and len(system.model.prompts) == 1
        assert system.kube.calls == []


def test_api_question_answer_replay_and_worker_resume(system):
    system.model.choose = lambda p: ask(p) if not p["history"] else stop(p)
    row = system.create()
    system.work()
    waiting = system.repo.latest(row["incident_id"])
    assert waiting["status"] == "waiting_user" and waiting["lease_owner"] is None
    with system.client() as (http, service):
        saved = service.get_run_snapshot(waiting).state
        q = saved["question"]
        assert q == waiting["question_payload"] and len(system.model.prompts) == 1
        path = f"/api/v1/incidents/{row['incident_id']}/runs/{row['run_id']}/answers"
        body = {"client_message_id": "human-input", "content": "change history", "question_id": q["question_id"],
                "version": q["version"], "answers": {"changes": "Updated an application setting"},
                "changed_resource_refs": [q["change_candidates"][0]["resource_ref"]]}
        assert http.post(path, json={**body, "question_id": "stale"}).status_code == 409
        assert http.post(path, json={**body, "changed_resource_refs": ["unregistered-resource"]}).status_code == 409
        accepted = http.post(path, json=body)
        assert accepted.status_code == 202, accepted.text
        assert http.post(path, json=body).json() == accepted.json()
        system.work()
        finished = system.repo.latest(row["incident_id"])
        assert finished["status"] == "succeeded" and len(system.model.prompts) == 2
        final = service.get_run_snapshot(finished).state
        assert final["diagnosis"]["fault_category"] == "unknown"
        assert final["answers"][0]["message_id"] in finished["adopted_message_ids"]
        assert http.post(path, json=body).json() == accepted.json()
        assert not system.kube.calls


@pytest.mark.parametrize("approved", [True, False])
def test_plan_waits_for_durable_approval_and_writes_at_most_once(system, approved):
    setup_drift(system)
    row = system.create()
    system.work()
    waiting = system.repo.latest(row["incident_id"])
    assert waiting["status"] == "waiting_approval"
    saved = snapshot(system, row)
    assert saved["remediation_plan"]["target_uid"] == "service-uid"
    assert system.kube.calls == [] and len(system.model.prompts) == 1
    decision = approve(system, row, saved, approved, via_api=True)
    system.work()
    final = system.repo.latest(row["incident_id"])
    assert final["status"] == "succeeded"
    assert final["output_snapshot"]["phase"] == ("verification_succeeded" if approved else "approval_rejected")
    assert len(system.kube.calls) == int(approved)
    assert system.verifier.verify.call_count == int(approved)
    system.repo.queue_approval(row["run_id"], decision, approval_binding(saved))  # Idempotent saved approval.
    system.work()
    assert len(system.kube.calls) == int(approved) and len(system.model.prompts) == 1
    if approved:
        assert system.repo.operation(row["run_id"])["state"] == "succeeded"
        if os.environ.get("INCIDENT_AGENT_TEST_AUDIT_DIR"):
            (Path(os.environ["INCIDENT_AGENT_TEST_AUDIT_DIR"]) / "production-flow.json").write_text(
                json.dumps({"providers": "controlled model/collector/Kubernetes; real PostgreSQL/checkpointer/worker",
                    "workflow_version": row["workflow_version"], "incident_id": row["incident_id"], "run_id": row["run_id"],
                    "waiting_phase": saved["phase"], "plan": saved["remediation_plan"],
                    "approval": decision, "terminal_phase": final["output_snapshot"]["phase"],
                    "model_calls": len(system.model.prompts), "writes": len(system.kube.calls),
                    "verification_calls": system.verifier.verify.call_count}, ensure_ascii=False, indent=2), encoding="utf-8")


def test_supplement_invalidates_old_approval_before_any_write(system):
    setup_drift(system)
    row = system.create()
    system.work()
    saved = snapshot(system, row)
    system.repo.control(row["incident_id"], ControlRequest(client_message_id="new-fact", content="Changed config", action="supplement"), row["run_id"], saved)
    with pytest.raises(ApprovalConflict): approve(system, row, saved)
    system.work()
    assert system.kube.calls == [] and system.repo.latest(row["incident_id"])["status"] == "cancelled"


def test_stop_while_waiting_rejects_late_answer(system):
    system.model.choose = ask
    row = system.create()
    system.work()
    saved = snapshot(system, row)
    q = saved["question"]
    system.repo.control(row["incident_id"], ControlRequest(client_message_id="stop-now", content="stop", action="stop"), row["run_id"], saved)
    with pytest.raises(RoundConflict):
        system.repo.answer(row["incident_id"], row["run_id"], AnswerQuestion(client_message_id="late", content="late",
            question_id=q["question_id"], version=q["version"], answers={"changes": "changed"}))
    system.work()
    assert len(system.model.prompts) == 1 and not system.kube.calls


@pytest.mark.parametrize("action", ["patch_service_selector", "patch_readiness_probe"])
def test_plan_parameters_come_from_registered_config_not_model(identified, action):
    drift(identified, action)
    plan = deterministic_plan(identified, action)
    assert plan.requires_approval and plan.target_uid
    if action == "patch_service_selector":
        assert {v.key: v.value for v in plan.parameters.proposed_selector} == {"app": "order-service"}
    else:
        assert plan.parameters.proposed_probe_path == "/readyz" and plan.parameters.proposed_probe_port == "http"
    identified["evidence"][5]["data"].update(status="failed", http_status=500, error_code="HTTP_STATUS_MISMATCH")
    with pytest.raises(ValueError): deterministic_plan(identified, action)


def test_switch_changes_only_new_runs_and_old_row_remains_readable(system):
    legacy = system.repo.accept(incident_id=str(uuid4()), run_id=str(uuid4()), thread_id=str(uuid4()),
        payload=system.state["request"], key=None, dialogue=False)
    assert legacy["workflow_version"] == "incident-v1"
    new = system.create()
    assert new["workflow_version"] == INVESTIGATION_WORKFLOW
    system.settings.investigation_enabled = False
    old_default = system.create()
    assert old_default["workflow_version"] == DIALOGUE_WORKFLOW
    with system.client() as (_, service):
        assert service.get_run_snapshot(legacy).state["request"] == legacy["input_payload"]
    assert system.repo.get_round(new["incident_id"], new["run_id"])["workflow_version"] == INVESTIGATION_WORKFLOW


def test_new_round_freezes_context_and_selects_new_workflow(system):
    from backend.app.persistence.messages import PostgresMessageRepository
    from backend.app.services.message_schemas import MessageDraft
    row = system.create()
    system.work()
    previous = snapshot(system, row)
    messages = PostgresMessageRepository(system.storage[0])
    message = messages.append(row["incident_id"], MessageDraft(client_message_id="new-round-input", content="Investigate again")).message
    child = system.repo.accept_round(row["incident_id"], message.message_id, "new-round", row["run_id"], previous)
    assert child["workflow_version"] == INVESTIGATION_WORKFLOW and child["thread_id"] != row["thread_id"]
    assert child["approval_payload"] is None
    system.work()
    assert system.repo.get_round(child["incident_id"], child["run_id"])["status"] == "succeeded"
    assert system.model.prompts[-1]["historical_context_unverified"]["messages"]


@pytest.mark.parametrize("kind", ["question", "conclusion", "approved_write"])
def test_crash_after_checkpoint_before_publication_reuses_results(system, kind):
    class ProcessStopped(BaseException):
        pass
    if kind == "question":
        system.model.choose = ask
    elif kind == "approved_write":
        setup_drift(system)
    row = system.create()
    if kind == "approved_write":
        system.work()
        approve(system, row, snapshot(system, row))
    interrupted = system.worker()
    def crash(*_):
        raise ProcessStopped()
    interrupted._project = crash
    lease = system.repo.claim(interrupted.owner, 60)
    with pytest.raises(ProcessStopped):
        interrupted.execute(lease)
    assert len(system.model.prompts) == 1
    assert len(system.kube.calls) == int(kind == "approved_write")
    with system.storage[0]() as connection:
        connection.execute("UPDATE incident_agent_app.run_budgets SET payload=jsonb_set(payload,'{seconds}','400'::jsonb) WHERE run_id=%s", (row["run_id"],))
    expire(system.storage[0], row["run_id"])
    system.settings.investigation_enabled = False  # Saved version, not current switch, controls recovery.
    system.work()
    finished = system.repo.latest(row["incident_id"])
    assert finished["status"] == ("waiting_user" if kind == "question" else "succeeded"), finished["last_error"]
    assert len(system.model.prompts) == 1
    assert len(system.kube.calls) == int(kind == "approved_write")
    assert system.verifier.verify.call_count == int(kind == "approved_write")


def test_enabled_investigation_rejects_sync_configuration():
    with pytest.raises(ValueError, match="INVESTIGATION_REQUIRES_QUEUED"):
        ApiSettings(_env_file=None, execution_mode="sync", investigation_enabled=True)


def test_human_wait_cannot_turn_old_evidence_into_a_write_plan(system):
    setup_drift(system)
    proposal = system.model.choose
    system.model.choose = lambda p: ask(p) if not p["history"] else proposal(p)
    row = system.create()
    system.work()
    q = snapshot(system, row)["question"]
    system.repo.answer(row["incident_id"], row["run_id"], AnswerQuestion(client_message_id="reply", content="reply",
        question_id=q["question_id"], version=q["version"], answers={"changes": "I changed config"}))
    system.work()
    final = snapshot(system, row)
    assert final["diagnosis"]["fault_category"] == "unknown"
    assert final["remediation_plan"] is None and not final["requires_approval"]
    assert len(system.model.prompts) == 3  # Question, invalid plan, one shared correction.
    assert system.kube.calls == []


def test_recovery_rejects_checkpoint_from_different_workflow(system):
    from backend.app.runtime.recovery import classify
    row = system.create()
    system.work()
    saved = snapshot(system, row)
    saved["workflow_version"] = "interactive-investigation-v2"
    decision = classify(system.repo.latest(row["incident_id"]), SimpleNamespace(
        values=saved, next=(), tasks=(), config={"configurable": {"checkpoint_id": "saved"}}))
    assert decision.status == "failed" and decision.error_code == "CHECKPOINT_WORKFLOW_MISMATCH"


def test_old_dialogue_uses_old_factory_even_when_new_flag_is_on(system, monkeypatch):
    import backend.app.runtime.worker as runtime
    from threading import Event
    system.settings.investigation_enabled = False
    row = system.create()
    system.settings.investigation_enabled = True
    marker = object()
    factory = Mock(return_value=marker)
    monkeypatch.setattr(runtime, "build_incident_graph", factory)
    for name in ("build_kubernetes_collector", "build_runbook_retriever", "build_diagnosis_service", "build_remediation_planner"):
        monkeypatch.setattr(runtime, name, lambda **_: Mock())
    lease = system.repo.claim("old-worker", 60)
    with production_graph(system.storage[2], system.repo, lease, Event()) as graph:
        assert graph is marker
    assert row["workflow_version"] == DIALOGUE_WORKFLOW and factory.call_args.kwargs["dialogue"] is True
    assert not system.model.prompts
