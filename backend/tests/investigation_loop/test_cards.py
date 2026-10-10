"""C1: source-preserving projections, malformed legacy data and actual JSONB reads."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from backend.app.investigation.cards import evidence_card, grouped_logs, saved_cards, VERSION
from backend.app.investigation.evidence import adapt
from backend.tests.runtime.test_worker_postgres import storage


def row(kind="PodLogs", data=None, **extra):
    return {"evidence_id": "ev-card-001", "resource_type": kind, "resource_name": "pod-a",
            "collected_at": "2026-10-10T01:00:00Z", "source": "test", "error": None,
            "data": data if data is not None else {"content": "timeout"}, **extra}


def test_exact_groups_keep_numbers_times_and_source_line_positions():
    logs = "2026-10-10T01:00:00Z HTTP 503\n2026-10-10T01:01:00Z HTTP 503\nHTTP 500\n"
    groups = grouped_logs(logs)["groups"]
    assert len(groups) == 2 and groups[0]["count"] == 2
    assert groups[0]["first_timestamp"] == "2026-10-10T01:00:00Z"
    assert groups[0]["last_timestamp"] == "2026-10-10T01:01:00Z"
    assert (groups[0]["first_line"], groups[0]["last_line"]) == (1, 2)
    invalid = grouped_logs("2026-99-99Tbad timeout\ntimeout")["groups"]
    assert len(invalid) == 2 and invalid[0]["first_timestamp"] is None


def test_redaction_or_control_cleanup_never_merges_different_messages():
    groups = grouped_logs("token=private-a\ntoken=private-b\nERR\n\x1b[31mERR")["groups"]
    assert len(groups) == 4 and all(g["count"] == 1 for g in groups)
    encoded = json.dumps(groups)
    assert "private-a" not in encoded and "private-b" not in encoded
    assert "\\u001b" not in encoded


def test_log_limits_preserve_counts_and_admit_omissions():
    result = grouped_logs("\n".join(f"message {i}" for i in range(12)))
    assert len(result["groups"]) == 8 and result["omitted_groups"] == 4
    assert result["scanned_lines"] == 12 and result["omitted_group_occurrences"] == 4
    result = grouped_logs("x" * 120001 + "\nlast")
    assert result["scanned_lines"] == 0 and result["omitted_lines"] == 2


def test_legacy_missing_metadata_stays_unknown_and_original_is_unchanged():
    item = row(data={"content": "timeout\n" * 3, "truncated": True, "previous": True})
    original = deepcopy(item)
    card = evidence_card(item, record_path="snapshot.evidence[0]")
    assert item == original and card["schema_version"] == VERSION
    assert card["identity"]["uid"] is None and card["identity"]["previous"] is True
    assert card["source_truncated"] and card["coverage"] == "unspecified_legacy_snapshot"
    assert card["observation_status"] != "healthy" and card["untrusted"]
    assert card["fields"]["logs"]["groups"][0]["count"] == 3


@pytest.mark.parametrize("kind,data", [
    ("PodStatus", {"containers": [None]}), ("EndpointSlice", {"endpoints": None}),
    ("PodLogs", {"content": []}), ("Service", []), ("Unregistered", {"anything": 1}),
    ("PodStatus", {"ready": "false"}), ("Deployment", {"ready_replicas": True}),
])
def test_malformed_or_unknown_rows_fall_back_without_health_claim(kind, data):
    card = evidence_card(row(kind, data))
    assert card["parse_status"] == "unparsed" and card["parse_reason"]
    assert card["fields"] == {} and card["excerpt"]


def test_error_and_partial_metadata_survive_even_when_fields_parse():
    card = evidence_card(row("BusinessCheck", {"status": "unknown", "error_code": "TIMEOUT"},
                             coverage="partial", truncated=True))
    assert card["error"] == "TIMEOUT" and card["source_truncated"]
    assert card["coverage"] == "partial" and card["observation_status"] == "failed_or_unknown"


def test_resource_projection_matches_actual_schema_and_marks_list_limits():
    item = row("Deployment", {"uid": "uid-a", "namespace": "agent-demo", "ready_replicas": 0,
        "containers": [{"name": f"c{i}", "requests": {"cpu": "10m"},
                        "args": ["--password", "do-not-export"], "readiness_probe": {"port": 8080}}
                       for i in range(25)]})
    card = evidence_card(item)
    assert card["identity"]["uid"] == "uid-a" and card["fields"]["ready_replicas"] == 0
    assert card["fields"]["containers"][0]["requests"] == {"cpu": "10m"}
    assert len(card["fields"]["containers"]) == 20
    assert {"path": "fields.containers", "items": 5} in card["projection_omissions"]
    assert "do-not-export" not in json.dumps(card)
    selection = evidence_card(row("PodSelection", {"service_pod_names": [], "namespace_pod_names": ["p"]}))
    assert selection["fields"]["namespace_pod_names"] == ["p"]


@pytest.mark.parametrize("tool,payload", [
    ("pod_events", [{"reason": "Unhealthy", "message": "probe failed", "count": 3}]),
    ("endpoint_slice", {"endpoints": [{"target_uid": "p", "conditions": {"ready": False}}]}),
    ("replica_set", {"replicas": 2, "ready_replicas": 0}),
])
def test_legacy_tool_text_parses_only_complete_saved_json(tool, payload):
    item = row("ToolObservation", {"tool": tool, "text": json.dumps(payload)})
    assert evidence_card(item)["parse_status"] == "parsed"
    item["truncated"] = True
    card = evidence_card(item)
    assert card["parse_status"] == "unparsed" and card["fields"] == {}
    item["truncated"] = False
    item["data"]["text"] = '{"partial":'
    assert evidence_card(item)["parse_reason"] == "TOOL_TEXT_NOT_JSON"


@pytest.mark.parametrize("tool", ["pod_logs", "pod_events", "endpoint_slice", "replica_set",
                                  "resource_summary", "deployment", "registered_business"])
def test_existing_tool_adapter_failures_keep_error_and_never_become_facts(tool):
    result = {"collected_at": "2026-10-10T01:00:00Z", "request_id": "r", "coverage": "unknown",
              "truncated": False, "error_code": "ACCESS_DENIED", "text": ""}
    ref = {"name": "p", "namespace": "agent-demo", "uid": "u", "container": "c",
           "service": "s", "deployment": "d"}
    item = adapt(result, {"tool": tool}, ref)[0]
    card = evidence_card(item)
    assert card["error"] == "ACCESS_DENIED" and card["observation_status"] == "failed_or_unknown"
    assert card["fields"] == {}


def test_saved_export_keeps_repeated_samples_and_limits_records():
    first = row()
    second = {**first, "collected_at": "2026-10-10T02:00:00Z"}
    snapshot = {"baseline": {"evidence": [first]}, "observations": [second]}
    report = saved_cards(snapshot, {})
    assert len(report["cards"]) == 2
    assert report["cards"][0]["source_ref"] != report["cards"][1]["source_ref"]
    assert saved_cards(snapshot, {}, limit=1)["omitted_records"] == 1
    assert saved_cards({"evidence": [first]}, {})["cards"]
    assert saved_cards(None, {"investigation_baseline": {"evidence": [first]}})["cards"]
    assert saved_cards(None, {})["cards"] == []


def test_actual_jsonb_round_trip_preserves_cards_and_source(storage):
    connect, _, _ = storage
    item = row("Service", {"uid": "u", "selector": {"z": "last", "a": "first"},
                           "ports": [{"target_port": 8080, "port": 80}]})
    original = deepcopy(item)
    with connect() as connection:
        restored = connection.execute("SELECT %s::jsonb AS evidence", (Jsonb(item),)).fetchone()["evidence"]
    assert evidence_card(item) == evidence_card(restored)
    assert item == original
    audit = os.environ.get("INCIDENT_AGENT_TEST_AUDIT_DIR")
    if audit:
        (Path(audit) / "evidence-cards.json").write_text(json.dumps({
            "schema_version": VERSION, "jsonb_round_trip": "passed", "source_unchanged": True,
            "card": evidence_card(restored), "live_calls": 0,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
