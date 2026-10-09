"""Durable baseline shared by investigation workflows, plus the internal read-only runner."""
from copy import deepcopy
from threading import Event
import time

from backend.app.agent.collector_adapter import normalize_evidence
from backend.app.agent.dependencies import build_kubernetes_collector, build_runbook_retriever
from backend.app.investigation.graph import build_investigation_graph
from backend.app.investigation.model import InvestigationModel
from backend.app.investigation.records import digest, saved_result
from backend.app.rag.query_builder import build_retrieval_query
from backend.app.runtime.budget import RunBudget, bind_budget
from backend.app.runtime.checkpointer import fenced_checkpointer
from backend.app.tools.investigation import build_investigation_toolbox
from backend.app.tools.deadline import read_budget
from backend.app.service_profiles.registry import load_profile


def _initial_read(budget, name, seconds, inputs, read):
    ticket, fresh = budget.reserve(name, seconds, request_id="6b1:" + name, fingerprint=digest(inputs))
    if not fresh:
        return saved_result(budget, ticket)
    start = time.monotonic()
    try:
        budget.repo.assert_owned(budget.lease)
        with read_budget(seconds):
            result = read()
    except Exception:
        budget.settle(ticket, time.monotonic() - start, status="failed_or_unknown")
        raise
    budget.settle(ticket, time.monotonic() - start, result=result)
    return result


def prepare_baseline(budget, *, collector=None, retriever=None):
    with budget.edit() as data:
        saved = deepcopy(data.get("investigation_baseline"))
    if saved is not None:
        return saved
    request = budget.lease["input_payload"]
    if request["namespace"] != "agent-demo":
        raise ValueError("TARGET_NAMESPACE_NOT_ALLOWED")
    load_profile(request["namespace"], request["service_name"])
    def collect():
        source = collector or build_kubernetes_collector(bounded_reads=True, include_details=False)
        bundle = source.collect(request["namespace"], request["service_name"])
        return {"incident_id": budget.lease["incident_id"], "request": request,
                "round_context": budget.lease.get("context_snapshot") or {},
                "service_profile": bundle.get("service_profile"),
                "evidence": normalize_evidence(incident_id=budget.lease["incident_id"], bundle=bundle)}
    baseline = _initial_read(budget, "investigation_baseline", 45, request, collect)
    # The query builder sorts evidence keys; replaying JSONB cannot change it.
    query = build_retrieval_query(baseline)
    baseline["retrieved_runbooks"] = _initial_read(budget, "investigation_retrieval", 30, query,
        lambda: (retriever or build_runbook_retriever()).retrieve(query))
    with budget.edit() as data:
        data["investigation_baseline"] = deepcopy(baseline)
    return baseline


def run_readonly_investigation(repository, lease, database_settings, *, lost=None, model=None):
    """Caller owns the run lease/heartbeat. Return result; never publish or write cluster state."""
    budget = RunBudget(repository, lease)
    with bind_budget(budget):
        baseline = prepare_baseline(budget)
        toolbox = build_investigation_toolbox(budget, baseline)
        with fenced_checkpointer(database_settings, repository, lease, lost or Event()) as saver:
            graph = build_investigation_graph(budget, toolbox, model or InvestigationModel(), checkpointer=saver)
            config = {"configurable": {"thread_id": lease["thread_id"]}, "recursion_limit": 20}
            snapshot = graph.get_state(config)
            if snapshot.values and snapshot.values.get("workflow_version") != "readonly-investigation-v1":
                # A crash before initialize may leave only this new graph's input.
                if snapshot.next != ("initialize",) or snapshot.values.get("baseline") != baseline:
                    raise ValueError("UNSUPPORTED_INVESTIGATION_CHECKPOINT")
            if snapshot.values and not snapshot.next:
                return snapshot.values
            with budget.activity():
                return graph.invoke(None if snapshot.values else {"baseline": baseline}, config)
