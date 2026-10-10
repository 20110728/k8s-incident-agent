"""Regression shape from the C2 browser failure; synthetic data, real request ledger."""
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.app.investigation.cards import evidence_card
from backend.app.investigation.context import build_context, estimate, INPUT_LIMIT
from backend.app.investigation.working_context import card_block
from backend.app.tools.log_text import decode_log_content
from backend.app.tools.pod_tools import get_pod_logs
from backend.app.tools.investigation import catalog
from backend.tests.investigation_loop.test_loop import case, storage, state, identified, toolbox, Model, stop
from backend.tests.investigation_loop.test_context_feedback import two_logs
from backend.tests.investigation_dialogue.test_dialogue import session


def rich_baseline(current):
    """Keep profile/UID bindings; add fields missing from earlier sparse fixtures."""
    for row in current["evidence"]:
        row["collected_at"] = "2026-10-10T02:52:25.052105+00:00"
        data = row["data"]
        if row["resource_type"] == "PodStatus":
            data.update(name=row["resource_name"], phase="Running", ready=False)
            data["containers"][0].update(image="docker.io/library/k8s-incident-demo:0.2.0", ready=False,
                restart_count=4, state="running", waiting_reason=None, waiting_message=None,
                terminated_reason=None, terminated_exit_code=None,
                last_terminated_reason="OOMKilled", last_terminated_exit_code=137)
        elif row["resource_type"] == "EndpointSlice":
            for endpoint in data["endpoints"]:
                endpoint.update(addresses=["10.244.1.10"], node_name="incident-agent-worker", ready=False,
                    serving=False, terminating=False, target_namespace="agent-demo", target_kind="Pod",
                    target_uid=endpoint["target_name"] + "-uid")
        elif row["resource_type"] == "Deployment":
            data.update(ready_replicas=0, available_replicas=0, unavailable_replicas=2)
            data["containers"][0].update(requests={"cpu": "20m", "memory": "32Mi"},
                                          limits={"cpu": "200m", "memory": "128Mi"})
        elif row["resource_type"] == "BusinessCheck":
            data.update(status="unknown", http_status=None, content_matches=None, error_code="CONNECTION_ERROR")
        elif row["resource_type"] == "OwnerChain":
            data["owner_chain"].update(direct_owner_kind="ReplicaSet", direct_owner_name="order-rs", direct_owner_uid="rs-uid")
    current["evidence"].extend([
        {"evidence_id": "ev-selection", "resource_type": "PodSelection", "resource_name": "order-service",
         "data": {"namespace": "agent-demo", "service_pod_names": ["order-pod", "order-pod-2"],
                  "namespace_pod_names": ["order-pod", "order-pod-2", "business-probe"]}},
        {"evidence_id": "ev-node", "resource_type": "Node", "resource_name": "incident-agent-worker",
         "data": {"name": "incident-agent-worker", "ready": True, "conditions": [
             {"condition_type": "MemoryPressure", "status": "False", "message": "kubelet has sufficient memory"}]}}
    ])


def test_bytes_are_decoded_before_redaction_grouping_and_next_model_call(case):
    budget, box, _ = case
    rich_baseline(box.state)
    # Production-sized object names, with consistent UID/owner relationships.
    renamed = json.dumps(box.state).replace("order-pod-2", "order-service-7b8c9d0e12-bbbbb").replace(
        "order-pod", "order-service-7b8c9d0e12-aaaaa")
    box.state = json.loads(renamed)
    box.refs = catalog(box.state, budget.lease["run_id"])
    budget.references(box.refs)
    line = '2026-10-10T02:52:34.123456789Z {"message":"GET /readyz 503","detail":"依赖不可用"}\n'
    box.clients.core.api.read_namespaced_pod_log.return_value = (line * 100).encode("utf-8")
    def choose(prompt):
        if not prompt["history"]:
            return two_logs(prompt)
        visible = set(prompt["available_evidence_ids"])
        facts = prompt["policy_facts"]
        assert set(facts["configuration_evidence_ids"] + facts["business_evidence_ids"]) <= visible
        logs = [json.loads(e["excerpt"]) for e in prompt["evidence"] if e["resource_type"] == "PodLogs"]
        assert len(logs) == 2
        assert all(log.get("content", "").count("\n") == 100 for log in logs)
        assert all(log["content"].startswith("2026-10-10T") for log in logs)
        return stop(prompt)
    model = Model(choose)
    with session(case, model) as (_, _, advance):
        result = advance()
    assert result["output"]["status"] == "stop" and len(model.prompts) == 2
    assert all(estimate(prompt) <= INPUT_LIMIT for prompt in model.prompts)
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 2
    assert all(e["data"]["content"].count("\n") == 100 for e in result["observations"])
    with session(case, Model(lambda _: pytest.fail("paid replay"))) as (_, _, advance):
        assert advance() == result
    assert box.clients.core.api.read_namespaced_pod_log.call_count == 2
    if os.environ.get("INCIDENT_AGENT_TEST_AUDIT_DIR"):
        (Path(os.environ["INCIDENT_AGENT_TEST_AUDIT_DIR"]) / "c2-fix.json").write_text(json.dumps({
            "controlled_model_attempts": 2, "tool_reads": 2, "input_estimates": [estimate(p) for p in model.prompts],
            "second_selected_ids": model.prompts[1]["available_evidence_ids"], "replay": "passed",
        }, indent=2), encoding="utf-8")


def test_current_and_historical_exit_fields_are_distinct(case):
    current = deepcopy(case[1].state)
    rich_baseline(current)
    pod = next(e for e in current["evidence"] if e["resource_type"] == "PodStatus")
    fields = json.loads(card_block(pod)["excerpt"])["containers"][0]
    assert fields["current"]["state"] == "running"
    assert fields["historical_only"]["last_terminated_reason"] == "OOMKilled"
    assert "last_terminated_reason" not in fields["current"]
    assert build_context(current, case[1].manifest(), [])["policy_facts"]["current_runtime_faults"] == []


def test_legacy_bytes_repr_is_not_silently_reinterpreted():
    item = {"resource_type": "PodLogs", "data": {"content": repr(b"line1\nline2\n")}}
    before = deepcopy(item)
    card = evidence_card(item)
    assert item == before and card["parse_status"] == "unparsed"
    assert card["parse_reason"] == "POSSIBLE_LEGACY_BYTES_REPR" and not card["fields"]


def test_baseline_logs_use_same_utf8_decoder():
    clients = SimpleNamespace(core=SimpleNamespace(read_namespaced_pod_log=Mock(return_value="中文\nnext\n".encode())))
    result = get_pod_logs(clients, "agent-demo", "pod")
    assert result.content == "中文\nnext\n" and not result.truncated
    assert decode_log_content("literal \\n text") == "literal \\n text"
    with pytest.raises(UnicodeDecodeError):
        decode_log_content(b"\xff")
    with pytest.raises(TypeError):
        decode_log_content({"body": "not text"})


def test_invalid_utf8_is_failed_observation_not_fake_log(case):
    box = case[1]
    box.clients.core.api.read_namespaced_pod_log.return_value = b"\xff"
    resource = next(k for k, ref in box.refs.items() if ref["kind"] == "pod")
    result = box.call({"tool": "pod_logs", "resource_ref": resource}, request_id="invalid-utf8")
    assert result["coverage"] == "unknown" and result["error_code"]
    assert "payload" not in result


def test_bytes_redaction_precedes_plain_text_limit(case):
    box = case[1]
    box.clients.core.api.read_namespaced_pod_log.return_value = (
        "Authorization: Bearer private-secret\n" + "line\n" * 50000).encode()
    resource = next(k for k, ref in box.refs.items() if ref["kind"] == "pod")
    result = box.call({"tool": "pod_logs", "resource_ref": resource}, request_id="large-bytes")
    assert result["payload"] == result["text"] and len(result["text"]) == 240000
    assert "private-secret" not in result["text"] and "\n" in result["text"]
    assert result["coverage"] == "partial" and result["truncated"]
