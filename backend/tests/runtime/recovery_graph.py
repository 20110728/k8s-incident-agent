"""Synthetic read-only graph for real-process crash tests; never calls a cluster/LLM."""
import time
from contextlib import contextmanager

from langgraph.graph import StateGraph, START, END

from backend.app.agent.state import IncidentState
from backend.app.runtime.checkpointer import fenced_checkpointer

NODES = ("collect_evidence", "retrieve_runbooks", "diagnose_incident", "plan_remediation")
PHASES = ("evidence_collected", "runbooks_retrieved", "diagnosis_completed", "remediation_skipped")


def context_factory(connect, repository, settings, blocked=None):
    @contextmanager
    def context(lease, lost):
        with fenced_checkpointer(settings, repository, lease, lost) as saver:
            builder = StateGraph(IncidentState)
            def node(name, phase):
                def run(state):
                    with connect() as connection:
                        connection.execute("INSERT INTO recovery_test_calls (node) VALUES (%s)", (name,))
                    if name == blocked:
                        time.sleep(60)
                    return {"phase": phase}
                return run
            previous = START
            for name, phase in zip(NODES, PHASES):
                builder.add_node(name, node(name, phase))
                builder.add_edge(previous, name)
                previous = name
            builder.add_edge(previous, END)
            yield builder.compile(checkpointer=saver)
    return context
