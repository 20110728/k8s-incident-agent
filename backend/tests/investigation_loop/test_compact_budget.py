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
    assert POLICY["total_tokens"] == 60000
    with budget.edit() as data:
        assert data["policy"]["total_tokens"] == 60000
        data["policy"].update(version="investigation-budget-v1", total_tokens=40000)
    with RunBudget(budget.repo, budget.lease).edit() as data:
        assert data["policy"]["total_tokens"] == 40000
    assert POLICY["total_tokens"] == 60000


@pytest.mark.parametrize("limit,used", [(40000, 22609), (60000, 42609)])
def test_remaining_budget_routes_directly_to_final_without_double_reservation(case, limit, used):
    budget, box, _ = case
    with budget.edit() as data:
        data["policy"]["total_tokens"] = limit
    budget.reserve("embedding", tokens=used)
    def choose(prompt):
        assert prompt["terminal_only"]
        return stop(prompt)
    model = Model(choose)
    with session(case, model) as (_, _, advance):
        result = advance()
    assert result["output"]["status"] == "stop" and len(model.prompts) == 1
    assert not result["observations"]
    box.clients.core.api.read_namespaced_pod_log.assert_not_called()
    with budget.edit() as data:
        calls = [c for c in data["calls"].values() if c["kind"] == "investigation_model"]
        assert len(calls) == 1 and calls[0]["metadata"]["purpose"] == "terminal"
        assert data["exhausted"] is None and data["tokens"] == used + 120
    with session(case, Model(lambda _: pytest.fail("paid final replay"))) as (_, _, advance):
        assert advance() == result


def test_correction_switches_to_final_only_after_cost_settlement(case):
    budget = case[0]
    with budget.edit() as data:
        data["policy"]["total_tokens"] = 40000
    budget.reserve("embedding", tokens=20000)
    class PaidModel(Model):
        def invoke(self, prompt):
            result = super().invoke(prompt)
            result["usage"] = {"total_tokens": 6000}
            return result
    def choose(prompt):
        if not prompt["feedback"]:
            assert not prompt["terminal_only"]
            return {**stop(prompt), "evidence_ids": ["ev-invented"]}
        assert prompt["terminal_only"]
        return stop(prompt)
    model = PaidModel(choose)
    with session(case, model) as (_, _, advance):
        result = advance()
    assert result["output"]["status"] == "stop" and len(model.prompts) == 2
    with budget.edit() as data:
        assert data["tokens"] == 32000 and data["exhausted"] is None


def test_final_only_still_rejects_collection_and_insufficient_single_call(case):
    budget, box, _ = case
    budget.reserve("embedding", tokens=45000)
    model = Model(collect)
    with session(case, model) as (_, _, advance):
        result = advance()
    assert result["output"]["stop_reason"] == "DECISION_VALIDATION_FAILED"
    assert all(p["terminal_only"] for p in model.prompts) and len(model.prompts) == 2
    box.clients.core.api.read_namespaced_pod_log.assert_not_called()


def test_cannot_afford_one_final_call_never_invokes_provider(case):
    budget = case[0]
    budget.reserve("embedding", tokens=51000)
    with session(case, Model(lambda _: pytest.fail("unfunded model call"))) as (_, _, advance):
        result = advance()
    assert result["output"]["stop_reason"] == "MODEL_TOKEN_LIMIT"


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
    assert estimate(prompt) <= INPUT_LIMIT
    assert all(len(h["reason"]) <= 120 and "missing_fact" not in h for h in prompt["history"])


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
    assert query == build_retrieval_query(json.loads(json.dumps(state, sort_keys=True)), compact=True)
