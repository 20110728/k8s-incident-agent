"""C2: explicit provenance, bounded context and paid-request recovery."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from backend.app.investigation.context import build_context, estimate, INPUT_LIMIT
from backend.app.investigation.evidence import current_state, adapt
from backend.app.investigation.graph import validate_decision
from backend.app.investigation.model import call_model
from backend.app.investigation.working_context import human_context, historical_context, unique_evidence
from backend.app.investigation.working_context import VERSION
from backend.tests.investigation_loop.test_loop import (
    case, storage, state, identified, toolbox, Model, run, stop,
)


def test_answer_binding_deduplicates_history_without_reclassifying_claim():
    answer = {"message_id": "m1", "question_id": "q1", "slot": "changes", "text": "I fixed it " * 200,
              "changed_resource_refs": ["ref-a"], "accepted_at": "2026-10-10T00:00:00Z"}
    raw = {"answers": [answer, deepcopy(answer)], "asked_slots": ["changes", "onset"]}
    before = deepcopy(raw)
    human = human_context(raw)
    assert raw == before and len(human["answers"]) == 1
    saved = human["answers"][0]
    assert saved["question_id"] == "q1" and saved["slot"] == "changes"
    assert not saved["text_truncated"] and saved["text"] == answer["text"]
    assert saved["source"] == "user_supplied_unverified"
    history = historical_context({"round_context": {"messages": [
        {"message_id": "m1", "content": answer["text"]}, {"message_id": "m2", "content": "old hypothesis"}]}}, human)
    assert [m["message_id"] for m in history["messages"]] == ["m2"]


def test_repeated_id_only_deduplicates_identical_content():
    row = {"evidence_id": "e1", "data": {"ready": True}}
    assert unique_evidence([row, deepcopy(row)]) == [row]
    with pytest.raises(ValueError, match="CONFLICTING_EVIDENCE_ID"):
        unique_evidence([row, {**row, "data": {"ready": False}}])


def test_current_view_respects_uid_namespace_time_and_keeps_source_records():
    old = {"evidence_id": "old", "resource_type": "PodStatus", "resource_name": "p",
           "collected_at": "2026-10-10T02:00:00Z", "data": {"uid": "u1", "namespace": "ns", "ready": True}}
    earlier = {**old, "evidence_id": "earlier", "collected_at": "2026-10-10T01:00:00Z"}
    replacement = {**old, "evidence_id": "replacement", "data": {**old["data"], "uid": "u2"}}
    source = {"evidence": [old]}
    prior = deepcopy(source)
    view = current_state(source, [earlier, replacement])
    assert source == prior
    assert [e["evidence_id"] for e in view["evidence"]] == ["old", "replacement"]
    assert view["historical_evidence_ids"] == ["earlier"]
    other = {**old, "evidence_id": "other-ns", "data": {**old["data"], "namespace": "other"}}
    assert len(current_state(source, [other])["evidence"]) == 2


def test_failed_refresh_and_human_report_never_restore_old_pass(case):
    _, box, _ = case
    ref = next(r for r in box.refs.values() if r["kind"] == "service")
    result = {"request_id": "failed", "collected_at": "2026-10-10T00:00:00Z", "coverage": "unknown",
              "truncated": False, "error_code": "ACCESS_DENIED", "text": ""}
    observations = adapt(result, {"tool": "registered_business"}, ref)
    current = current_state(box.state, observations)
    prompt = build_context(current, box.manifest(), [], dialogue={"asked_slots": ["changes"], "answers": [
        {"message_id": "answer", "slot": "changes", "question_id": "q", "text": "Everything is fixed"}]})
    assert prompt["policy_facts"]["business_status"] == "unknown"
    assert prompt["working_state"]["snapshot_status"] == "requires_new_baseline_for_health_after_human_wait"
    assert prompt["working_state"]["failed_evidence_ids"] == [observations[0]["evidence_id"]]
    old_id = next(e["evidence_id"] for e in box.state["evidence"] if e["resource_type"] == "BusinessCheck")
    assert old_id in prompt["working_state"]["historical_evidence_ids"]
    assert old_id not in prompt["available_evidence_ids"]
    with pytest.raises(ValueError, match="EVIDENCE_NOT_IN_CONTEXT"):
        validate_decision({"decision": {**stop(prompt), "evidence_ids": [old_id]}}, prompt, current, box, False)


def test_card_context_is_valid_json_single_representation_and_round_trip_stable(case, storage):
    current = deepcopy(case[1].state)
    for row in current["evidence"]:
        row["data"]["irrelevant_padding"] = "UNNEEDED-BODY" * 10000
    original = deepcopy(current)
    prompt = build_context(current, case[1].manifest(), [])
    assert estimate(prompt) > 0 and current == original
    assert "UNNEEDED-BODY" in json.dumps(prompt)
    assert all(e["projection"] in {"evidence-card-v1", "saved-evidence-raw-v1"} and json.loads(e["excerpt"]) for e in prompt["evidence"])
    assert len(prompt["available_evidence_ids"]) == len(set(prompt["available_evidence_ids"]))
    with storage[0]() as connection:
        restored = connection.execute("SELECT %s::jsonb AS state", (Jsonb(current),)).fetchone()["state"]
    assert build_context(restored, case[1].manifest(), []) == prompt
    assert build_context(current, case[1].manifest(), [], terminal_only=True)["purpose"] == "diagnosis"
    audit = os.environ.get("INCIDENT_AGENT_TEST_AUDIT_DIR")
    if audit:
        (Path(audit) / "working-context.json").write_text(json.dumps({
            "context_version": prompt["context_version"], "input_estimate": estimate(prompt),
            "input_limit": INPUT_LIMIT, "selected_ids": prompt["available_evidence_ids"],
            "jsonb_stable": True, "live_calls": 0,
        }, indent=2), encoding="utf-8")


def test_paid_call_replays_and_persists_selection_without_raw_bodies(case):
    budget, box, _ = case
    prompt = build_context(box.state, box.manifest(), [])
    model = Model(stop)
    first = call_model(model, budget, prompt, "c2:paid")
    second = call_model(Model(lambda _: pytest.fail("paid replay")), budget,
                        json.loads(json.dumps(prompt, sort_keys=True)), "c2:paid")
    assert first == second and len(model.prompts) == 1
    with budget.edit() as data:
        calls = [c for c in data["calls"].values() if c["kind"] == "investigation_model"]
        assert len(calls) == 1
        metadata = calls[0]["metadata"]
        assert metadata["context_version"] == VERSION and metadata["selection"]
        assert all("excerpt" not in item for item in metadata["selection"])


def test_old_input_limit_no_longer_discards_evidence_or_blocks_provider(case, monkeypatch):
    from backend.app.investigation import context
    monkeypatch.setattr(context, "INPUT_LIMIT", 1)
    model = Model(stop)
    result = run(case, model)
    assert result["output"]["status"] == "stop" and len(model.prompts) == 1
    assert not model.prompts[0]["omitted_evidence_ids"]
    with case[0].edit() as data:
        assert not data.get("context_assembly_failures")


def test_new_investigation_history_snapshot_preserves_all_message_text():
    from datetime import datetime, UTC
    from backend.app.services.round_context import build_round_context
    messages = [{"message_id": f"m{n}", "sequence": n, "source": "user", "created_at": datetime.now(UTC),
                 "content": "historical claim " * 1000} for n in range(12, 0, -1)]
    previous = {"diagnosis": {"root_cause": "old hypothesis " * 1000}}
    saved = build_round_context(messages, previous, "prior", "legacy", full=True)
    assert len(saved["messages"]) == 12 and not saved["older_messages_omitted"]
    assert all(not m["truncated"] and m["content"] == messages[0]["content"] for m in saved["messages"])
    assert json.loads(saved["previous_result"]["diagnosis_excerpt"]) == previous["diagnosis"]
    assert "never current evidence" in historical_context({"round_context": saved}, None)["usage"]
