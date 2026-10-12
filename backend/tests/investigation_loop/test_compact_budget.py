"""Bounded context retention and final-only routing with real persisted budgets."""
from copy import deepcopy
import json

import pytest

from backend.app.investigation.compact import log_excerpt
from backend.app.investigation.context import build_context, estimate, INPUT_LIMIT
from backend.app.rag.query_builder import build_retrieval_query
from backend.app.runtime.budget import POLICY, RunBudget
from backend.tests.investigation_loop.test_loop import (
    case, storage, state, identified, toolbox, Model, stop, collect,
)
from backend.tests.investigation_dialogue.test_dialogue import session
from backend.tests.investigation_loop.test_context_feedback import crowd_baseline


def test_new_policy_and_existing_budget_snapshot_are_distinct(case):
    budget = case[0]
    assert POLICY["total_tokens"] is None and POLICY["decisions"] == 5
    assert POLICY["model_attempts"] == 6
    with budget.edit() as data:
        assert data["policy"]["total_tokens"] is None
        data["policy"].update(version="investigation-budget-v1", total_tokens=40000)
    with RunBudget(budget.repo, budget.lease).edit() as data:
        assert data["policy"]["total_tokens"] == 40000
    assert POLICY["total_tokens"] is None


def test_large_usage_time_and_many_tools_do_not_block_new_run(case):
    budget, box, _ = case
    budget.reserve("embedding", seconds=10000, tokens=1000000)
    for n in range(10):
        budget.reserve("tool", seconds=1000, extra=True, key=f"prior-{n}")
    model = Model(lambda prompt: collect(prompt) if not prompt["history"] else stop(prompt))
    with session(case, model) as (_, _, advance):
        result = advance()
    assert result["output"]["status"] == "stop" and len(model.prompts) == 2
    assert not any(p["terminal_only"] for p in model.prompts)
    assert len(result["observations"]) == 1
    with budget.edit() as data:
        assert data["tokens"] > 1000000 and data["exhausted"] is None
    budget.before_write()
    with session(case, Model(lambda _: pytest.fail("paid replay"))) as (_, _, advance):
        assert advance() == result


def test_correction_not_forced_to_final_by_token_cost(case):
    budget = case[0]
    budget.reserve("embedding", tokens=1000000)
    def choose(prompt):
        assert not prompt["terminal_only"]
        return stop(prompt) if prompt["feedback"] else {**stop(prompt), "evidence_ids": ["ev-invented"]}
    model = Model(choose)
    with session(case, model) as (_, _, advance):
        result = advance()
    assert result["output"]["status"] == "stop" and len(model.prompts) == 2
    with budget.edit() as data:
        assert data["tokens"] == 1000240 and data["exhausted"] is None


def test_removed_cost_limit_does_not_allow_invented_citations(case):
    budget, box, _ = case
    budget.reserve("embedding", tokens=1000000)
    model = Model(lambda prompt: {**stop(prompt), "evidence_ids": ["ev-invalid"]})
    with session(case, model) as (_, _, advance):
        result = advance()
    assert result["output"]["stop_reason"] == "DECISION_VALIDATION_FAILED"
    assert len(model.prompts) == 2
    box.clients.core.api.read_namespaced_pod_log.assert_not_called()


def test_all_prior_samples_and_core_baseline_remain_after_next_batch(case):
    _, box, _ = case
    current = deepcopy(box.state)
    crowd_baseline(current)
    samples = []
    for n in range(4):
        row = {"evidence_id": f"ev-sample-{n}", "resource_type": "PodLogs" if n < 2 else "ToolObservation",
               "resource_name": f"pod-{n}", "request_id": f"sample-{n}", "coverage": "partial", "error": None,
               "data": {"content": "dependency refused\n" * 1000, "container_name": "order-service", "previous": False}
                   if n < 2 else {"tool": "pod_events", "text": "Readiness probe 503\n" * 200}}
        samples.append(row)
    current["evidence"].extend(samples)
    history = [{"step": i + 1, "action": "collect", "reason": "长理由" * 190,
        "missing_fact": "缺少信息" * 140, "results": [{"tool": "pod_logs", "evidence_ids": [s["evidence_id"]]}
        for s in samples[i * 2:i * 2 + 2]]} for i in range(2)]
    prompt = build_context(current, box.manifest(), history)
    visible = set(prompt["available_evidence_ids"])
    assert {s["evidence_id"] for s in samples} <= visible
    facts = prompt["policy_facts"]
    assert set(facts["business_evidence_ids"] + facts["configuration_evidence_ids"] + facts["resource_evidence_ids"]) <= visible
    assert estimate(prompt) > 0
    assert prompt["history"] == history


def test_logs_group_exact_messages_and_retrieval_is_byte_bounded(case):
    content = "2026-10-09T09:00:00Z dependency refused\n2026-10-09T09:01:00Z dependency refused\n"
    result = log_excerpt(content)
    assert result["groups"][0]["count"] == 2
    assert "09:00" in result["groups"][0]["first"] and "09:01" in result["groups"][0]["last"]
    state = deepcopy(case[1].state)
    crowd_baseline(state)
    original = deepcopy(state)
    query = build_retrieval_query(state, compact=True)
    assert len(query.encode("utf-8")) <= 2400 and "BusinessCheck" in query
    assert len(query) < len(build_retrieval_query(state))
    assert state == original
    restored = json.loads(json.dumps(state, sort_keys=True))
    assert query == build_retrieval_query(restored, compact=True)
    # The same projection also feeds paid model prompts, whose request
    # fingerprints must not change solely because JSONB reordered nested keys.
    manifest = case[1].manifest()
    assert build_context(state, manifest, []) == build_context(restored, manifest, [])
