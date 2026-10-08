"""6A: durable reservations with real PostgreSQL; no live model or cluster."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from backend.app.persistence.controls import ControlRepository
from backend.app.persistence.leases import LeaseLost
from backend.app.runtime.budget import BudgetExceeded, RunBudget, bind_budget, budget_view, invoke_model
from backend.app.runtime.worker import OwnedDependency
from backend.app.tools.investigation import ToolRequest
from backend.tests.runtime.test_worker_postgres import storage, accept, expire
from backend.tests.interactions.test_interactions import client_for
from backend.tests.runtime.test_operations_postgres import operation_case


@pytest.fixture
def budget(storage):
    repo = ControlRepository(storage[0])
    accept(repo)
    return RunBudget(repo, repo.claim("budget-test", 60))


def view(budget):
    return budget_view(budget.repo, budget.lease["incident_id"], budget.lease["run_id"])


def test_crash_reservation_and_question_survive_reclaim(storage, budget):
    ticket = budget.reserve("model", 60, tokens=14000)
    budget.decision("question:1")
    expire(storage[0], budget.lease["run_id"])
    lease = budget.repo.claim("replacement", 60)
    resumed = RunBudget(budget.repo, lease)
    resumed.decision("question:1")
    assert view(resumed)["used"] == dict(active_seconds=60, extra_seconds=0, tokens=14000, decisions=1, tools=0)
    with pytest.raises(LeaseLost):
        budget.settle(ticket, 0, tokens=0)
    resumed.reserve("model", 60, tokens=14000)
    with pytest.raises(BudgetExceeded, match="MODEL_TOKEN_LIMIT"):
        resumed.reserve("model", tokens=14000)
    assert view(resumed)["handoff"]["reason"] == "MODEL_TOKEN_LIMIT"


def test_simultaneous_reservations_cannot_overspend(budget):
    def reserve(_):
        try:
            budget.reserve("model", tokens=14000)
            return True
        except BudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(4))) == 2
    assert view(budget)["used"]["tokens"] == 28000


def test_tools_and_decisions_stop_but_last_collection_can_be_summarized(budget):
    for n in range(3):
        budget.decision(str(n))
    with pytest.raises(BudgetExceeded, match="DECISION_LIMIT"):
        budget.decision("fourth")
    for n in range(6):
        budget.reserve("tool", 15, extra=True, key=str(n))
    with pytest.raises(BudgetExceeded, match="DUPLICATE"):
        budget.reserve("tool", 15, extra=True, key="0")
    with pytest.raises(BudgetExceeded, match="TOOL_REQUEST_LIMIT"):
        budget.reserve("tool", 15, extra=True, key="7")
    budget.reserve("model", 60, tokens=14000)
    assert view(budget)["used"]["extra_seconds"] == 90


def test_elapsed_time_refunds_reservation_and_human_wait_is_not_charged(budget, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("backend.app.runtime.budget.time.monotonic", lambda: clock[0])
    with budget.activity():
        clock[0] += 2
        with budget.stage("collect", 45):
            clock[0] += 10
        clock[0] += 3
    assert view(budget)["used"]["active_seconds"] == 15
    clock[0] += 86400
    with budget.stage("plan", 60):
        clock[0] += 1
    assert view(budget)["used"]["active_seconds"] == 16


def test_time_caps_and_no_new_write_without_verification_reserve(budget):
    ticket = budget.reserve("collect", 151)
    budget.settle(ticket, 151)
    tool = Mock()
    with bind_budget(budget), pytest.raises(BudgetExceeded, match="WRITE_VERIFICATION"):
        OwnedDependency(tool, budget.repo, budget.lease, Event()).execute({})
    tool.execute.assert_not_called()
    budget.reserve("collect", 149)
    with pytest.raises(BudgetExceeded, match="ACTIVE_TIME"):
        budget.reserve("collect", 1)
    assert budget.repo.operation(budget.lease["run_id"]) is None


def test_extra_time_limit_applies_even_when_tool_count_is_not_full(budget):
    ticket = budget.reserve("tool", 15, extra=True, key="slow")
    budget.settle(ticket, 90)  # Record actual timeout overrun, do not hide it.
    with pytest.raises(BudgetExceeded, match="ACTIVE_TIME_LIMIT"):
        budget.reserve("tool", 15, extra=True, key="second")
    assert view(budget)["used"]["tools"] == 1


def test_dispatched_operation_projection_is_not_blocked_by_budget(budget, monkeypatch):
    budget.reserve("collect", 300)
    monkeypatch.setattr(budget.repo, "operation", lambda _: {"state": "dispatching"})
    reconciler = SimpleNamespace(execute=Mock(return_value="requires-reconciliation"))
    with bind_budget(budget):
        assert OwnedDependency(reconciler, budget.repo, budget.lease, Event()).execute({}) == "requires-reconciliation"
    assert reconciler.execute.call_count == 1


@pytest.mark.parametrize("usage,charge", [({"total_tokens": 123}, 123), ({}, 14000), ({"total_tokens": 0}, 14000)])
def test_provider_usage_or_unknown_charge(budget, usage, charge):
    runnable = Mock()
    runnable.invoke.return_value = {"raw": SimpleNamespace(usage_metadata=usage)}
    with bind_budget(budget):
        invoke_model(runnable, [("human", "diagnose")], ToolRequest)
    assert view(budget)["used"]["tokens"] == charge
    assert view(budget)["calls"][0]["metadata"]["input_limit_is_estimated"] is True


def test_failed_and_oversize_model_calls_do_not_get_free_retries(budget):
    runnable = Mock()
    runnable.invoke.side_effect = TimeoutError()
    with bind_budget(budget), pytest.raises(TimeoutError):
        invoke_model(runnable, [("human", "diagnose")], ToolRequest)
    assert view(budget)["used"]["tokens"] == 14000
    with bind_budget(budget), pytest.raises(BudgetExceeded, match="INPUT_ESTIMATE"):
        invoke_model(runnable, [("human", "a" * 40000)], ToolRequest)
    assert runnable.invoke.call_count == 1
    assert view(budget)["exhausted"] == "MODEL_INPUT_ESTIMATE_LIMIT"


def test_budget_api_is_read_only_and_bound_to_incident(budget):
    url = f'/api/v1/incidents/{budget.lease["incident_id"]}/runs/{budget.lease["run_id"]}/budget'
    with client_for(budget.repo) as client:
        assert client.get(url).json()["available"] is False
        budget.reserve("collect", 45)
        assert client.get(url).json()["used"]["active_seconds"] == 45
        assert client.get(url.replace(budget.lease["incident_id"], str(uuid4()))).status_code == 404
        assert client.post(url, json={"tokens": 0}).status_code == 405


def test_unknown_cost_and_saved_completion_can_publish_at_exhaustion(storage):
    from backend.tests.interactions.test_interactions import seed, body, run_worker
    repo, incident, refs = seed(storage)
    repo.accept_interaction(incident, body("explain"), None, refs)
    lease = repo.claim("first", 60)
    budget = RunBudget(repo, lease)
    budget.reserve("model", 300, tokens=40000)
    from backend.tests.interactions.test_interactions import FakeModel
    model = FakeModel()
    explanation, _ = model.call("explain", "why", refs)
    repo.save_progress(lease, {"explain": explanation})
    expire(storage[0], lease["run_id"])
    run_worker(repo)  # Model is forbidden; only saved explanation is published.
    assert repo.get_round(incident, lease["run_id"])["status"] == "succeeded"


def test_prepared_patch_is_not_dispatched_without_remaining_verification_time(operation_case):
    from backend.app.runtime.operations import LedgerExecutor
    _, repo, state, lease, kube = operation_case
    budget = RunBudget(repo, lease)
    budget.reserve("earlier_activity", 181)
    with bind_budget(budget), pytest.raises(BudgetExceeded):
        LedgerExecutor(kube.clients, repo, lease).execute(state)
    assert kube.calls == []
    assert repo.operation(lease["run_id"])["state"] == "prepared"


def test_uncertain_write_is_reconciled_after_budget_exhaustion(operation_case):
    from backend.app.runtime.operations import LedgerExecutor, OutcomeUnknown
    _, repo, state, lease, kube = operation_case
    executor = LedgerExecutor(kube.clients, repo, lease)
    kube.mode = "timeout_applied"
    with pytest.raises(OutcomeUnknown):
        executor.execute(state)
    budget = RunBudget(repo, lease)
    budget.reserve("earlier_activity", 300, tokens=40000)
    with bind_budget(budget):
        executor.reconcile(repo.operation(lease["run_id"]))
    assert len(kube.calls) == 1
    assert repo.operation(lease["run_id"])["state"] != "outcome_unknown"


def test_langgraph_nodes_receive_durable_budget_context(budget):
    from langgraph.graph import StateGraph, START, END
    from backend.app.runtime.budget import CURRENT
    graph = StateGraph(dict)
    def node(state):
        assert CURRENT.get() is budget
        budget.reserve("model", tokens=14000)
        return state
    graph.add_node("diagnose", node)
    graph.add_edge(START, "diagnose")
    graph.add_edge("diagnose", END)
    with bind_budget(budget):
        graph.compile().invoke({"input": "controlled"})
    assert view(budget)["used"]["tokens"] == 14000


def test_budgeted_sdk_construction_bounds_output_and_disables_retries(monkeypatch):
    from backend.app.llm import client
    from backend.app.rag import embeddings
    chat, embedding = Mock(), Mock()
    monkeypatch.setattr(client, "ChatOpenAI", chat)
    monkeypatch.setattr(embeddings, "OpenAIEmbeddings", embedding)
    settings = SimpleNamespace(llm_model="controlled", llm_timeout_seconds=90, llm_max_retries=4,
        dashscope_api_key=SimpleNamespace(get_secret_value=lambda: "test"), dashscope_base_url="http://unused.invalid",
        embedding_model="controlled", embedding_dimensions=1024)
    with bind_budget(object()):
        client.build_chat_model(settings)
        embeddings.build_embeddings(settings)
    assert chat.call_args.kwargs["max_retries"] == 0
    assert chat.call_args.kwargs["timeout"] == 60 and chat.call_args.kwargs["max_tokens"] == 2000
    assert embedding.call_args.kwargs["max_retries"] == 0
    assert embedding.call_args.kwargs["request_timeout"] == 25
