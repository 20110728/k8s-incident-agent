"""Public investigation projection, real API history, and budget privacy."""
from copy import deepcopy
import json
from types import SimpleNamespace

from backend.app.investigation.presentation import investigation_view
from backend.app.api.routes.rounds import get_round_repository
from backend.app.api.routes.interactions import get_interaction_repository
from backend.app.runtime.budget import budget_view
from backend.app.services.round_context import INVESTIGATION_WORKFLOW
from backend.tests.investigation_production.test_production import system, storage, state, identified, toolbox
from backend.tests.investigation_dialogue.test_dialogue import ask


def test_projection_keeps_failed_resampling_and_old_citations_without_raw_payload():
    old = {"evidence_id": "old", "resource_type": "PodLogs", "resource_name": "pod", "data": {"content": "PRIVATE-LOG"}}
    fresh = {**old, "evidence_id": "new", "coverage": "unknown", "error": "READ_FAILED", "truncated": True}
    saved = {"workflow_version": INVESTIGATION_WORKFLOW, "baseline": {"evidence": [old]}, "observations": [fresh],
        "evidence": [fresh], "history": [{"step": 1, "action": "collect", "reason": "Authorization: Bearer private-value",
            "missing_fact": "current logs", "evidence_ids": ["old"], "results": [{"tool": "pod_logs", "coverage": "unknown",
                "error_code": "READ_FAILED", "evidence_ids": ["new"], "sampling": {"generation": 1, "query_key": "INTERNAL"}}]}],
        "output": {"status": "handoff", "stop_reason": "DECISION_VALIDATION_FAILED", "validation_failures": [
            {"attempt": 1, "stage": "policy", "action": "collect", "detail": "RESOURCE_NOT_IN_CONTEXT", "private": "INTERNAL"}]}, "prompt": "PRIVATE-PROMPT"}
    before = deepcopy(saved)
    view = investigation_view(saved)
    assert saved == before
    assert [item["current"] for item in view["observations"]] == [False, True]
    assert view["steps"][0]["results"][0]["generation"] == 1
    assert view["observations"][1]["error"] == "READ_FAILED"
    assert view["validation_failures"][0]["detail"] == "RESOURCE_NOT_IN_CONTEXT"
    assert not any(secret in json.dumps(view) for secret in ("PRIVATE-LOG", "PRIVATE-PROMPT", "INTERNAL", "private-value"))
    assert investigation_view({"history": saved["history"]}) is None


def test_projection_bounds_old_records_and_does_not_invent_completion():
    saved = {"workflow_version": INVESTIGATION_WORKFLOW, "history": [{"step": 1, "action": "collect"}],
        "observations": [{"evidence_id": str(i), "error": "x" * 2000} for i in range(70)]}
    view = investigation_view(saved)
    assert len(view["observations"]) == 64 and view["omitted_observations"] == 6
    assert view["outcome"] is None and view["steps"][0]["results"] == []
    assert len(view["observations"][0]["error"]) == 800


def test_budget_counts_unknown_usage_without_exposing_replay_results():
    payload = {"policy": {}, "seconds": 1, "extra_seconds": 0, "tokens": 9520, "decisions": [], "tools": [], "exhausted": None,
        "calls": {"one": {"kind": "investigation_model", "usage": {"total_tokens": 20}, "charged_tokens": 20, "result": {"prompt": "PRIVATE"}},
                  "two": {"kind": "investigation_model", "status": "started_or_interrupted", "reserved_tokens": 9500,
                          "fingerprint": "INTERNAL", "future_private_field": "PRIVATE"},
                  "three": {"kind": "embedding", "usage": {"total_tokens": 9}}}}
    before = deepcopy(payload)
    repo = SimpleNamespace(get_round=lambda incident, run: None, _read=lambda *_: [{"payload": payload}])
    view = budget_view(repo, "incident", "run")
    assert view["generation"] == {"attempts": 2, "reported_tokens": 20, "unreported_attempts": 1}
    assert view["used"]["tokens"] == 9520 and payload == before
    assert view["accounting"]["reported_charge"] == 20
    assert view["accounting"]["estimated_or_reserved_charge"] == 9500
    assert "PRIVATE" not in json.dumps(view) and "INTERNAL" not in json.dumps(view)


def test_api_projects_waiting_and_terminal_round_without_new_model_calls(system):
    system.model.choose = ask
    row = system.create()
    system.work()
    with system.client() as (http, _):
        http.app.dependency_overrides[get_round_repository] = lambda: system.repo
        http.app.dependency_overrides[get_interaction_repository] = lambda: system.repo
        path = f"/api/v1/incidents/{row['incident_id']}/runs/{row['run_id']}"
        first = http.get(path)
        assert first.status_code == 200, first.text
        result = first.json()["result"]
        assert result["investigation"]["steps"][0]["action"] == "ask_user"
        q = result["run"]["question"]
        assert q["change_candidates"]
        for _ in range(2):
            assert http.get(path).json()["result"]["investigation"] == result["investigation"]
        assert len(system.model.prompts) == 1
        response = http.post(path + "/answers", json={"client_message_id": "skip-view-test", "content": "skip",
            "question_id": q["question_id"], "version": q["version"], "answers": {}, "skip": True})
        assert response.status_code == 202, response.text
        system.work()
        final = http.get(path).json()["result"]
        assert final["investigation"]["stop_reason"] == "HUMAN_QUESTION_SKIPPED"
        assert final["investigation"]["answer_count"] == 1 and len(system.model.prompts) == 1
        public_budget = http.get(path + "/budget").json()
        assert public_budget["generation"]["attempts"] == 1
        assert all("result" not in call for call in public_budget["calls"])
