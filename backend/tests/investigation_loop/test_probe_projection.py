"""Exact probe folding, exception preservation, provenance and paid replay."""
from copy import deepcopy
import json

import pytest

from backend.app.investigation.context import build_context
from backend.app.investigation.log_projection import project_logs, encoded_bytes
from backend.app.investigation.working_context import (
    structured_block, run_context_version, VERSION, LEGACY_VERSION,
)
from backend.app.investigation.model import call_model
from backend.tests.investigation_loop.test_loop import (
    case, storage, state, identified, toolbox, Model, stop, collect, conclusion,
)
from backend.tests.investigation_dialogue.test_dialogue import session


def access(second, path="readyz", status=503):
    return f'2026-10-11T13:30:{second:02d}Z ' + json.dumps({
        "service": "order", "message": f'"GET /{path} HTTP/1.1" {status} -',
    }) + "\n"


def test_probe_groups_cover_sample_without_losing_exception_text_or_order():
    error = '2026-10-11T13:30:20Z {"event":"dependency_request_failed","errno":111,"detail":"原文"}\n'
    lines = [access(n) for n in range(20)] + [error, 'unparsed traceback\r\n']
    lines += [access(n, "livez", 200) for n in range(20, 40)]
    lines += [access(n) for n in range(40, 60)] + ['last partial line']
    content = ''.join(lines)
    view = project_logs(content)
    assert view and view["sampled_lines"] == len(lines) and view["omitted_lines"] == 0
    assert [g["count"] for g in view["probe_groups"]] == [40, 20]
    assert view["probe_groups"][0]["first_timestamp"] == '2026-10-11T13:30:00Z'
    assert view["probe_groups"][0]["last_timestamp"] == '2026-10-11T13:30:59Z'
    assert view["probe_groups"][0]["sample_raw"] == lines[0]
    covered = []
    for group in view["probe_groups"]:
        positions = [i for start, end in group["line_ranges"] for i in range(start, end + 1)]
        assert len(positions) == group["count"]
        covered += positions
    for span in view["raw_spans"]:
        assert span["content"] == ''.join(lines[span["start_line"] - 1:span["end_line"]])
        covered += list(range(span["start_line"], span["end_line"] + 1))
    assert sorted(covered) == list(range(1, len(lines) + 1))
    assert error in ''.join(s["content"] for s in view["raw_spans"])
    assert encoded_bytes({"log_view": view}) < encoded_bytes({"content": content})


@pytest.mark.parametrize('line', [
    'traceback with /readyz 503\n',
    '{"event":"dependency_request_failed","timestamp":"2026-10-11T13:30:00Z"}\n',
    '{"service":"order","message":"\\\"GET /readyz HTTP/1.1\\\" 503 -","detail":"error"}\n',
    '{"service":"order","message":"\\\"GET /api/orders HTTP/1.1\\\" 500 -"}\n',
    '2026-99-99T00:00:00Z {"service":"order","message":"\\\"GET /readyz HTTP/1.1\\\" 503 -"}\n',
    '{"service":"order","message":"hidden","message":"\\\"GET /readyz HTTP/1.1\\\" 503 -"}\n',
    '{malformed json\n',
])
def test_unknown_and_diagnostic_lines_are_never_grouped(line):
    assert project_logs(line * 1000) is None


def test_small_sample_falls_back_to_raw_if_projection_would_grow():
    assert project_logs(access(0) + access(1)) is None


def test_deep_unparseable_json_is_preserved():
    assert project_logs('{"message":' + '[' * 2000 + '0' + ']' * 2000 + '}\n') is None


def test_real_exchange_shape_keeps_each_dependency_failure_and_does_not_double_encode():
    from scripts.compare_investigation_context import compare
    failures = [f'2026-10-11T13:30:{n % 60:02d}Z ' + json.dumps({
        "timestamp": f"2026-10-11T13:30:{n % 60:02d}.123456+00:00",
        "service": "order", "event": "dependency_request_failed", "purpose": "readiness",
        "target_host": "order-dependency.agent-demo.svc.cluster.local", "reason": "request_error",
        "http_status": None, "exception_type": "URLError", "cause_type": "ConnectionRefusedError", "errno": 111,
    }) + '\n' for n in range(230)]
    content = ''.join(failure + access(n % 60) for n, failure in enumerate(failures))
    content += ''.join(access(n % 60, "livez", 200) for n in range(86))
    body = {"content": content, "previous": False, "container_name": "order"}
    projected = structured_block({"evidence_id": "ev-log", "resource_type": "PodLogs", "data": body})
    view = projected["data"]["log_view"]
    assert view["sampled_lines"] == 546
    assert ''.join(s["content"] for s in view["raw_spans"]) == ''.join(failures)
    prompt = {"context_version": LEGACY_VERSION, "available_evidence_ids": ["ev-log"], "evidence": [
        {"evidence_id": "ev-log", "resource_type": "PodLogs", "excerpt": json.dumps(body)}]}
    report = {"input": {"messages": [{"role": "system", "content": "unchanged"},
                                      {"role": "user", "content": json.dumps(prompt)}]}}
    before = deepcopy(report)
    result = compare(report)
    assert result["evidence_ids_unchanged"] and report == before
    assert result["logs"][0]["verbatim_lines"] == 230
    assert result["logs"][0]["grouped_probe_lines"] == 316
    assert result["new_evidence_utf8_bytes"] < result["old_evidence_utf8_bytes"]


def test_structured_body_preserves_source_identity_and_truncation():
    item = {"evidence_id": "ev-log", "resource_type": "PodLogs", "resource_name": "pod-a",
            "collected_at": "2026-10-11T13:31:00Z", "coverage": "partial",
            "data": {"uid": "u1", "container_name": "order", "previous": True, "truncated": True,
                     "content": ''.join(access(n % 60) for n in range(1000))}}
    before = deepcopy(item)
    result = structured_block(item)
    assert item == before
    assert result["source_truncated"] and result["previous"] is True
    assert result["data"]["uid"] == "u1" and result["coverage"] == "partial"
    assert "excerpt" not in result and "content" not in result["data"]
    assert result["data"]["log_view"]["probe_groups"][0]["count"] == 1000
    assert result == structured_block(json.loads(json.dumps(item, sort_keys=True)))
    failed = structured_block({**item, "error": "ACCESS_DENIED", "coverage": "unknown"})
    assert failed["error"] == "ACCESS_DENIED" and failed["coverage"] == "unknown"


def test_native_json_keeps_fields_and_removes_only_duplicate_payload_text():
    item = {"evidence_id": "ev-tool", "resource_type": "ToolObservation", "data": {
        "payload": {"detail": 'quoted "value"', "token": "private-value"},
        "payload_complete": True, "text": "duplicate-body",
    }}
    before = deepcopy(item)
    result = structured_block(item)
    assert item == before
    assert result["data"]["payload"]["detail"] == 'quoted "value"'
    assert "text" not in result["data"] and "private-value" not in json.dumps(result)


def test_legacy_paid_run_keeps_original_prompt_and_replays(case):
    budget, box, _ = case
    prompt = build_context(box.state, box.manifest(), [], context_version=LEGACY_VERSION)
    assert prompt["working_state"]["version"] == LEGACY_VERSION
    assert prompt["context_view"] == "full_raw"
    assert all("excerpt" in e and "data" not in e for e in prompt["evidence"])
    first = call_model(Model(stop), budget, prompt, "legacy:paid")
    assert run_context_version(budget) == LEGACY_VERSION
    rebuilt = build_context(box.state, box.manifest(), [], context_version=run_context_version(budget))
    assert rebuilt == prompt
    assert call_model(Model(lambda _: pytest.fail("paid replay")), budget, rebuilt, "legacy:paid") == first


def test_new_run_saves_actual_structured_message_and_body_hash(case):
    budget, box, _ = case
    assert run_context_version(budget) == VERSION
    prompt = build_context(box.state, box.manifest(), [])
    call_model(Model(stop), budget, prompt, "structured:paid")
    with budget.edit() as data:
        saved = data["last_model_input"]["input"]
        assert json.loads(saved["messages"][1]["content"]) == prompt
        metadata = next(c["metadata"] for c in data["calls"].values() if c["kind"] == "investigation_model")
        assert all(s["body_digest"] and s["source_digest"] for s in metadata["selection"])
    assert run_context_version(budget) == VERSION


def test_grouped_probe_context_reaches_next_decision_without_mutating_saved_logs(case):
    budget, box, _ = case
    error = '2026-10-11T13:31:00Z {"event":"dependency_request_failed","cause_type":"ConnectionRefusedError"}\n'
    content = ''.join(access(n % 60) for n in range(999)) + error
    box.clients.core.api.read_namespaced_pod_log.return_value = content

    def choose(prompt):
        if not prompt["history"]:
            return collect(prompt)
        logs = [r for r in prompt["evidence"] if r["resource_type"] == "PodLogs"]
        view = logs[0]["data"]["log_view"]
        assert view["sampled_lines"] == 1000
        assert view["probe_groups"][0]["count"] == 999
        assert view["raw_spans"] == [{"start_line": 1000, "end_line": 1000, "content": error}]
        assert logs[0]["evidence_id"] in prompt["available_evidence_ids"]
        return conclusion(prompt, "unknown")

    model = Model(choose)
    with session(case, model) as (_, _, advance):
        result = advance()
    assert result["output"]["status"] == "conclude" and len(model.prompts) == 2
    log = next(r for r in result["observations"] if r["resource_type"] == "PodLogs")
    assert log["data"]["content"] == content
    with session(case, Model(lambda _: pytest.fail("paid replay"))) as (_, _, advance):
        assert advance() == result
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 1
