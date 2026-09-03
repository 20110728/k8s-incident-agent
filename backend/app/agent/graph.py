from langgraph.graph import END, START, StateGraph

from backend.app.agent.collector_adapter import (
    EvidenceCollectorPort,
)
from backend.app.agent.nodes import (
    finish_failure,
    make_collect_evidence_node,
    make_diagnose_incident_node,
    make_plan_remediation_node,
    make_retrieve_runbooks_node,
    plan_collection,
    skip_remediation,
    validate_request,
    prepare_approval,
    request_human_approval,
)

from backend.app.agent.state import IncidentState
from backend.app.llm.diagnoser import (
    DiagnosisServicePort,
)
from backend.app.llm.remediation_planner import (
    RemediationPlannerPort,
)
from backend.app.rag.retriever import (
    RunbookRetrieverPort,
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


def route_after_retrieval(
    state: IncidentState,
) -> str:
    if state.get("phase") == "runbooks_retrieved":
        return "diagnose"

    return "stop"


def route_after_diagnosis(
    state: IncidentState,
) -> str:
    if state.get("phase") != "diagnosis_completed":
        return "stop"

    diagnosis = state.get("diagnosis") or {}
    fault_category = diagnosis.get(
        "fault_category"
    )

    if fault_category in {
        "unknown",
        "no_fault_detected",
    }:
        return "skip"

    return "plan"

def route_after_remediation(
    state: IncidentState,
) -> str:
    if state.get("phase") != "remediation_planned":
        return "stop"

    if state.get("requires_approval") is True:
        return "approval"

    return "finish"


def route_after_prepare_approval(
    state: IncidentState,
) -> str:
    if (
        state.get("phase") == "awaiting_approval"
        and state.get("approval_status") == "pending"
    ):
        return "request"

    return "stop"

def build_incident_graph(
    collector: EvidenceCollectorPort,
    retriever: RunbookRetrieverPort | None = None,
    diagnoser: DiagnosisServicePort | None = None,
    planner: RemediationPlannerPort | None = None,
    *,
    checkpointer=None,
):
    if diagnoser is not None and retriever is None:
        raise ValueError(
            "diagnoser requires a runbook retriever"
        )

    if planner is not None and diagnoser is None:
        raise ValueError(
            "planner requires a diagnoser"
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

    builder.add_edge(
        "finish_failure",
        END,
    )

    if retriever is None:
        builder.add_edge(
            "collect_evidence",
            END,
        )
        return builder.compile(
            checkpointer=checkpointer,
        )

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
        return builder.compile(
            checkpointer=checkpointer,
        )

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

    if planner is None:
        builder.add_edge(
            "diagnose_incident",
            END,
        )
        return builder.compile(
            checkpointer=checkpointer,
        )

    builder.add_node(
        "plan_remediation",
        make_plan_remediation_node(planner),
    )
    builder.add_node(
        "skip_remediation",
        skip_remediation,
    )
    builder.add_node(
        "prepare_approval",
        prepare_approval,
    )
    builder.add_node(
        "request_human_approval",
        request_human_approval,
    )

    builder.add_conditional_edges(
        "diagnose_incident",
        route_after_diagnosis,
        {
            "plan": "plan_remediation",
            "skip": "skip_remediation",
            "stop": END,
        },
    )

    builder.add_edge(
        "skip_remediation",
        END,
    )

    builder.add_conditional_edges(
        "plan_remediation",
        route_after_remediation,
        {
            "approval": "prepare_approval",
            "finish": END,
            "stop": END,
        },
    )

    builder.add_conditional_edges(
        "prepare_approval",
        route_after_prepare_approval,
        {
            "request": "request_human_approval",
            "stop": END,
        },
    )

    # Day 13仅记录审批结果。
    # Day 14将在批准分支后接入白名单执行节点。
    builder.add_edge(
        "request_human_approval",
        END,
    )

    return builder.compile(
        checkpointer=checkpointer,
    )