from langgraph.graph import END, START, StateGraph

from backend.app.agent.collector_adapter import (
    EvidenceCollectorPort,
)
from backend.app.agent.nodes import (
    finish_failure,
    make_collect_evidence_node,
    make_diagnose_incident_node,
    make_retrieve_runbooks_node,
    plan_collection,
    validate_request,
)
from backend.app.agent.state import IncidentState
from backend.app.rag.retriever import (
    RunbookRetrieverPort,
)
from backend.app.llm.diagnoser import (
    DiagnosisServicePort,
)

def route_after_validation(
    state: IncidentState,
) -> str:
    if state.get("valid") is True:
        return "continue"

    return "reject"


def route_after_collection(
    state: IncidentState,
) -> str:
    if state.get("phase") in {
        "evidence_collected",
        "evidence_collected_with_errors",
    }:
        return "retrieve"

    return "stop"


def build_incident_graph(
    collector: EvidenceCollectorPort,
    retriever: RunbookRetrieverPort | None = None,
    diagnoser: DiagnosisServicePort | None = None,
):

    if diagnoser is not None and retriever is None:
        raise ValueError(
            "diagnoser requires a runbook retriever"
        )

    builder = StateGraph(IncidentState)

    builder.add_node(
        "validate_request",
        validate_request,
    )
    builder.add_node(
        "plan_collection",
        plan_collection,
    )
    builder.add_node(
        "collect_evidence",
        make_collect_evidence_node(collector),
    )
    builder.add_node(
        "finish_failure",
        finish_failure,
    )

    builder.add_edge(
        START,
        "validate_request",
    )

    builder.add_conditional_edges(
        "validate_request",
        route_after_validation,
        {
            "continue": "plan_collection",
            "reject": "finish_failure",
        },
    )

    builder.add_edge(
        "plan_collection",
        "collect_evidence",
    )

    if retriever is None:
        builder.add_edge(
            "collect_evidence",
            END,
        )
    else:
        builder.add_node(
            "retrieve_runbooks",
            make_retrieve_runbooks_node(retriever),
        )

        builder.add_conditional_edges(
            "collect_evidence",
            route_after_collection,
            {
                "retrieve": "retrieve_runbooks",
                "stop": END,
            },
        )

        if diagnoser is None:
            builder.add_edge(
                "retrieve_runbooks",
                END,
            )
        else:
            builder.add_node(
                "diagnose_incident",
                make_diagnose_incident_node(diagnoser),
            )

            builder.add_conditional_edges(
                "retrieve_runbooks",
                route_after_retrieval,
                {
                    "diagnose": "diagnose_incident",
                    "stop": END,
                },
            )

            builder.add_edge(
                "diagnose_incident",
                END,
            )

        builder.add_edge(
            "finish_failure",
            END,
        )

    return builder.compile()

def route_after_retrieval(
    state: IncidentState,
) -> str:
    if state.get("phase") == "runbooks_retrieved":
        return "diagnose"

    return "stop"