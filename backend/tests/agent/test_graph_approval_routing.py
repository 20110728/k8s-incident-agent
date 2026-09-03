from langgraph.checkpoint.memory import InMemorySaver

from backend.app.agent.graph import (
    build_incident_graph,
    route_after_prepare_approval,
    route_after_remediation,
)


def test_approved_action_routes_to_approval():
    result = route_after_remediation(
        {
            "phase": "remediation_planned",
            "requires_approval": True,
        }
    )

    assert result == "approval"


def test_manual_plan_finishes_without_approval():
    result = route_after_remediation(
        {
            "phase": "remediation_planned",
            "requires_approval": False,
        }
    )

    assert result == "finish"


def test_failed_remediation_plan_stops():
    result = route_after_remediation(
        {
            "phase": "remediation_failed",
            "requires_approval": None,
        }
    )

    assert result == "stop"


def test_pending_approval_routes_to_interrupt_node():
    result = route_after_prepare_approval(
        {
            "phase": "awaiting_approval",
            "approval_status": "pending",
        }
    )

    assert result == "request"


def test_invalid_approval_state_stops():
    result = route_after_prepare_approval(
        {
            "phase": "approval_failed",
            "approval_status": "failed",
        }
    )

    assert result == "stop"


def test_full_graph_contains_approval_nodes():
    graph = build_incident_graph(
        collector=object(),
        retriever=object(),
        diagnoser=object(),
        planner=object(),
        checkpointer=InMemorySaver(),
    )

    node_names = set(graph.get_graph().nodes)

    assert "plan_remediation" in node_names
    assert "prepare_approval" in node_names
    assert "request_human_approval" in node_names