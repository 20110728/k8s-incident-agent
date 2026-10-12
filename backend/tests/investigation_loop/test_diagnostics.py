"""Failed decisions are explainable without re-invoking a provider or loosening rules."""
from copy import deepcopy
from functools import partial
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.investigation.contracts import Decision
from backend.app.investigation.diagnostics import debug_report, error_detail, provider_diagnostics
from backend.app.persistence.database import connect_database
from backend.app.persistence.interactions import InteractionRepository
from backend.app.persistence.rounds import RoundNotFound
from backend.app.runtime.budget import budget_view
from backend.tests.investigation_loop.test_loop import case, storage, state, identified, toolbox, Model, run, stop


def test_failed_policy_attempts_saved_once_and_replayed_without_provider(case):
    model = Model(lambda p: {**stop(p), "evidence_ids": ["ev-invented"]})
    first = run(case, model)
    failures = first["output"]["validation_failures"]
    assert len(model.prompts) == 2 and [x["attempt"] for x in failures] == [1, 2]
    assert all(x["stage"] == "policy" and x["detail"] == "EVIDENCE_NOT_IN_CONTEXT" for x in failures)
    assert not first["history"] and not first["observations"]
    replay = Model(lambda _: pytest.fail("diagnostic replay invoked provider"))
    assert run(case, replay) == first
    budget = case[0]
    # Match the API repository, using the same isolated test database. The
    # worker fixture's OperationRepository does not provide get_round().
    view_repo = InteractionRepository(partial(connect_database, case[2]))
    view = budget_view(view_repo, budget.lease["incident_id"], budget.lease["run_id"])
    assert view["generation"]["attempts"] == 2
    assert len([c for c in view["calls"] if c.get("validation")]) == 2
    assert all("validation_context" not in c and "result" not in c for c in view["calls"])
    with pytest.raises(RoundNotFound):
        budget_view(view_repo, str(uuid4()), budget.lease["run_id"])


def test_schema_diagnostics_omit_rejected_input():
    with pytest.raises(ValueError) as caught:
        Decision.model_validate({"decision": {"action": "BAD-ACTION-private-password"}})
    detail = error_detail(caught.value)
    assert "union_tag_invalid" in detail
    assert "private-password" not in detail


def test_parse_failure_captures_finish_reason_and_bounded_redacted_final_content():
    raw = SimpleNamespace(content='Authorization: Bearer private-key ' + 'x' * 4000,
        response_metadata={"finish_reason": "length", "private": "not-exported"},
        additional_kwargs={"reasoning_content": "HIDDEN-REASONING"})
    result = provider_diagnostics({"raw": raw, "parsing_error": ValueError("raw output might contain private-key")})
    assert result["finish_reason"] == "length" and result["output_excerpt_truncated"]
    assert len(result["output_excerpt"]) <= 3000
    assert "private-key" not in json.dumps(result) and "HIDDEN-REASONING" not in json.dumps(result)


def test_old_record_export_is_read_only_and_explicit_about_missing_details():
    row = {"incident_id": "event", "run_id": "run", "status": "succeeded", "output_snapshot": {
        "baseline": {"secret": "DO-NOT-EXPORT"}, "output": {"stop_reason": "DECISION_VALIDATION_FAILED"}}}
    data = {"calls": {"old": {"kind": "investigation_model", "status": "completed",
        "result": {"parsed": None, "parse_error": "STRUCTURED_OUTPUT_INVALID"}}}}
    prior = deepcopy((row, data))
    result = debug_report(row, data)
    assert result["model_attempts"][0]["validation"] is None
    assert result["model_attempts"][0]["result"]["parse_error"] == "STRUCTURED_OUTPUT_INVALID"
    assert result["limits"] and (row, data) == prior
    assert "DO-NOT-EXPORT" not in json.dumps(result)


def test_cli_exports_requested_incident_using_read_only_transaction(case, monkeypatch, tmp_path, capsys):
    from scripts import export_investigation_debug as cli
    budget = case[0]
    run(case, Model(lambda p: {**stop(p), "evidence_ids": ["bad"]}))
    monkeypatch.setattr(cli, "get_database_settings", lambda: case[2])
    monkeypatch.setattr("sys.argv", ["export", "--incident-id", budget.lease["incident_id"]])
    monkeypatch.chdir(tmp_path)
    cli.main()
    exported = list((tmp_path / "evals/results/investigation-debug").glob("*.json"))
    assert len(exported) == 1
    report = json.loads(exported[0].read_text(encoding="utf-8"))
    assert report["run"]["run_id"] == budget.lease["run_id"]
    assert report["model_attempts"][0]["validation"]["detail"] == "EVIDENCE_NOT_IN_CONTEXT"
    assert budget.lease["run_id"] in capsys.readouterr().out


def test_cli_optional_cards_read_old_baseline_without_mutating_budget(case, monkeypatch, tmp_path):
    from scripts import export_investigation_debug as cli
    budget, box, settings = case
    with budget.edit() as data:
        data["investigation_baseline"] = deepcopy(box.state)
    with budget.edit() as data:
        before = deepcopy(data)
    monkeypatch.setattr(cli, "get_database_settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["export", "--incident-id", budget.lease["incident_id"],
                                    "--run-id", budget.lease["run_id"], "--evidence-cards"])
    monkeypatch.chdir(tmp_path)
    cli.main()
    exported = list((tmp_path / "evals/results/investigation-debug").glob("*.json"))
    report = json.loads(exported[0].read_text(encoding="utf-8"))
    cards = report["evidence_cards"]["cards"]
    assert len(cards) == len(box.state["evidence"])
    assert cards[0]["source_ref"]["record_path"] == "budget.investigation_baseline.evidence[0]"
    assert not report["model_attempts"] and not report["tool_attempts"]
    with budget.edit() as data:
        assert data == before
    box.clients.core.api.read_namespaced_pod_log.assert_not_called()
    monkeypatch.setattr("sys.argv", ["export", "--incident-id", str(uuid4()),
                                    "--run-id", budget.lease["run_id"], "--evidence-cards"])
    with pytest.raises(ValueError, match="INCIDENT_OR_RUN_NOT_FOUND"):
        cli.main()
    assert len(list((tmp_path / "evals/results/investigation-debug").glob("*.json"))) == 1


def test_brief_keeps_failure_and_usage_without_duplicate_bodies():
    from backend.app.investigation.brief_debug import brief_debug_report
    row = {"incident_id": "event", "run_id": "run", "status": "succeeded", "output_snapshot": {
        "output": {"stop_reason": "REQUIRED_EVIDENCE_NOT_IN_CONTEXT"}}}
    data = {"tokens": 19000, "calls": {"one": {"kind": "investigation_model", "status": "completed",
        "request_id": "decision:1", "usage": {"total_tokens": 123},
        "metadata": {"selection": [{"evidence_id": str(i), "excerpt_digest": "x" * 64} for i in range(100)],
                     "context_version": "v2.2", "input_estimate": 1234},
        "validation": {"detail": "DUPLICATE_QUERY"},
        "result": {"parsed": {"decision": {"action": "collect", "reason": "Authorization: Bearer private-key"}}}}},
        "context_assembly_failures": {"decision:2": {"reason": "REQUIRED_EVIDENCE_NOT_IN_CONTEXT",
                                                  "omitted_ids": ["ev-required"]}}}
    before = deepcopy((row, data))
    brief = brief_debug_report(row, data)
    encoded = json.dumps(brief)
    assert "private-key" not in encoded and "excerpt_digest" not in encoded
    assert brief["model_attempts"][0]["validation"] == "DUPLICATE_QUERY"
    assert brief["context_failures"][0]["omitted_ids"] == ["ev-required"]
    assert brief["budget"]["reported_model_tokens"] == 123
    assert brief["budget"]["charged_or_reserved_tokens"] == 19000
    assert len(encoded) < len(json.dumps(debug_report(row, data))) / 2
    assert (row, data) == before


def test_brief_old_record_does_not_invent_success():
    from backend.app.investigation.brief_debug import brief_debug_report
    result = brief_debug_report({"status": "running"}, {"calls": {
        "old": {"kind": "investigation_model", "status": "started_or_interrupted"}}})
    assert result["stop_reason"] is None and not result["evidence"]
    assert result["budget"]["model_calls_missing_usage"] == 1


def test_cli_brief_includes_card_status_without_full_cards(case, monkeypatch, tmp_path):
    from scripts import export_investigation_debug as cli
    budget, box, settings = case
    with budget.edit() as data:
        data["investigation_baseline"] = deepcopy(box.state)
    monkeypatch.setattr(cli, "get_database_settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["export", "--incident-id", budget.lease["incident_id"],
                                    "--brief", "--evidence-cards"])
    monkeypatch.chdir(tmp_path)
    cli.main()
    path, = (tmp_path / "evals/results/investigation-debug").glob("*-brief.json")
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["report_version"] == "brief-v1" and report["evidence"]
    assert "evidence_cards" not in report and "available_resources" not in report
    assert all("parse" in e and "fields" not in e and "excerpt" not in e for e in report["evidence"])
    box.clients.core.api.read_namespaced_pod_log.assert_not_called()
