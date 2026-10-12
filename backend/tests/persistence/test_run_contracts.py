import base64
import json
from datetime import UTC, datetime

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.app.agent.schemas import IncidentRequest
from backend.app.api.dependencies import build_incident_service
from backend.app.config import ApiSettings
from backend.app.persistence.runs import (
    InvalidRunQuery, QueuedModeRequired, decode_cursor, encode_cursor,
    request_digest, run_summary, validate_key,
)
from backend.app.services.incident_service import IncidentApplicationService
from backend.tests.api.fakes import FakeIncidentGraph, FakeIncidentRepository


def test_digest_uses_normalized_input_without_collapsing_inner_spaces():
    request = IncidentRequest(namespace="default", service_name="demo", description=" 问题  描述 ")
    payload = request.model_dump(mode="json")
    assert payload["description"] == "问题  描述"
    assert request_digest(payload) == request_digest(dict(reversed(list(payload.items()))))
    assert request_digest(payload) != request_digest({**payload, "description": "问题 描述"})


@pytest.mark.parametrize("key", ["", "has space", "中文", "x" * 129, "a\n"])
def test_invalid_keys(key):
    with pytest.raises(InvalidRunQuery):
        validate_key(key)


def test_cursor_binds_scope_and_preserves_tie_breaker():
    row = {"created_at": datetime.now(UTC), "incident_id": "incident-1"}
    cursor = encode_cursor("incidents", row, "incident_id")
    assert decode_cursor(cursor, "incidents") == (row["created_at"], "incident-1")
    with pytest.raises(InvalidRunQuery):
        decode_cursor(cursor, "runs:incident-1")
    for bad in ["!", "a", "e30", "x" * 1025]:
        with pytest.raises(InvalidRunQuery):
            decode_cursor(bad, "incidents")
    naive = {"v": 1, "scope": "incidents", "created_at": "2026-01-01", "id": "a"}
    with pytest.raises(InvalidRunQuery):
        decode_cursor(base64.urlsafe_b64encode(json.dumps(naive).encode()).decode().rstrip("="), "incidents")


def test_sync_rejects_key_before_persistence_or_execution():
    graph, repository = FakeIncidentGraph(), FakeIncidentRepository()
    service = IncidentApplicationService(graph, repository)
    with pytest.raises(QueuedModeRequired):
        service.create_incident(IncidentRequest(namespace="default", service_name="demo", description="test"), idempotency_key="key")
    assert not graph.invocations


def test_queued_builder_does_not_construct_external_dependencies(monkeypatch):
    from backend.app.api import dependencies
    def forbidden():
        pytest.fail("queued API constructed an execution dependency")
    for name in ("build_kubernetes_collector", "build_runbook_retriever", "build_diagnosis_service",
                 "build_remediation_planner", "build_remediation_executor", "build_recovery_verifier"):
        monkeypatch.setattr(dependencies, name, forbidden)
    service = build_incident_service(checkpointer=InMemorySaver(), repository=FakeIncidentRepository(),
                                     runs=object(), execution_mode="queued")
    assert service is not None
    assert ApiSettings(execution_mode="queued").execution_mode == "queued"


def test_summary_does_not_expose_input_key_or_exception_text():
    row = dict(run_id="r", status="failed", run_kind="diagnosis", created_at="t", updated_at="t",
               finished_at=None, attempt=1, last_error={"code": "postgres://secret", "message": "secret"},
               input_payload={"secret": "value"}, idempotency_key="private")
    summary = run_summary(row)
    assert summary["last_error_code"] is None
    assert "secret" not in str(summary) and "private" not in str(summary)
