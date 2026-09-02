import re

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from backend.app.agent.schemas import Diagnosis, IncidentRequest
from backend.app.agent.state import IncidentState

from collections.abc import Callable

from backend.app.agent.collector_adapter import (
    EvidenceCollectorPort,
    normalize_evidence,
)

from backend.app.rag.query_builder import (
    build_retrieval_query,
)
from backend.app.rag.retriever import (
    RunbookRetrieverPort,
)

from backend.app.llm.diagnoser import (
    DiagnosisServicePort,
)

ALLOWED_NAMESPACE = "agent-demo"

REFERENCE_EDGE_CHARACTERS = " \t\r\n,，;；"

EVIDENCE_ID_PATTERN = re.compile(
    (
        r"(?<![a-zA-Z0-9-])"
        r"ev-[a-zA-Z0-9-]+-\d{3}"
        r"(?![a-zA-Z0-9-])"
    )
)


class InvalidDiagnosisReference(ValueError):
    pass


def _normalize_reference_ids(values: list[str]) -> list[str]:
    normalized: list[str] = []

    for value in values:
        cleaned = value.strip(REFERENCE_EDGE_CHARACTERS)

        if not cleaned:
            raise InvalidDiagnosisReference(
                "diagnosis contains an empty reference ID"
            )

        if cleaned not in normalized:
            normalized.append(cleaned)

    return normalized

def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def trace_event(
    step: str,
    status: str,
    message: str,
) -> dict[str, str]:
    return {
        "step": step,
        "status": status,
        "message": message,
        "timestamp": utc_now(),
    }


def validate_request(state: IncidentState) -> dict[str, Any]:
    incident_id = state.get("incident_id") or str(uuid4())
    raw_request = state.get("request", {})

    try:
        request = IncidentRequest.model_validate(raw_request)
    except ValidationError as exc:
        return {
            "incident_id": incident_id,
            "phase": "validation_failed",
            "valid": False,
            "error_count": state.get("error_count", 0) + 1,
            "errors": [
                {
                    "stage": "validate_request",
                    "code": "INVALID_REQUEST",
                    "message": str(exc),
                }
            ],
            "trace": [
                trace_event(
                    "validate_request",
                    "failed",
                    "request schema validation failed",
                )
            ],
        }

    if request.namespace != ALLOWED_NAMESPACE:
        return {
            "incident_id": incident_id,
            "request": request.model_dump(),
            "phase": "validation_failed",
            "valid": False,
            "error_count": state.get("error_count", 0) + 1,
            "errors": [
                {
                    "stage": "validate_request",
                    "code": "NAMESPACE_NOT_ALLOWED",
                    "message": (f"namespace {request.namespace!r} is not allowed"),
                }
            ],
            "trace": [
                trace_event(
                    "validate_request",
                    "failed",
                    "namespace rejected by safety policy",
                )
            ],
        }

    return {
        "incident_id": incident_id,
        "request": request.model_dump(),
        "phase": "validated",
        "valid": True,
        "error_count": state.get("error_count", 0),
        "trace": [
            trace_event(
                "validate_request",
                "completed",
                "request validated",
            )
        ],
    }


def plan_collection(state: IncidentState) -> dict[str, Any]:
    plan = [
        "get_service",
        "list_namespace_pod_names",
        "list_service_pod_names",
        "get_service_endpoint_slices",
        "get_pod_status",
        "get_pod_events",
        "get_pod_logs",
        "resolve_pod_owner",
        "get_deployment_config",
        "get_node_status",
    ]

    return {
        "phase": "collection_planned",
        "collection_plan": plan,
        "evidence": state.get("evidence", []),
        "trace": [
            trace_event(
                "plan_collection",
                "completed",
                f"planned {len(plan)} read-only operations",
            )
        ],
    }


def finish_failure(state: IncidentState) -> dict[str, Any]:
    return {
        "phase": "failed",
        "trace": [
            trace_event(
                "finish_failure",
                "completed",
                "workflow stopped because request was rejected",
            )
        ],
    }


def make_collect_evidence_node(
    collector: EvidenceCollectorPort,
) -> Callable[[IncidentState], dict[str, Any]]:
    def collect_evidence(
        state: IncidentState,
    ) -> dict[str, Any]:
        incident_id = state["incident_id"]

        try:
            request = IncidentRequest.model_validate(state["request"])

            bundle = collector.collect(
                namespace=request.namespace,
                service_name=request.service_name,
            )

            evidence = normalize_evidence(
                incident_id=incident_id,
                bundle=bundle,
            )

            collector_errors = bundle.get("errors", [])

            if not evidence:
                return {
                    "phase": "evidence_collection_failed",
                    "error_count": (state.get("error_count", 0) + 1),
                    "errors": [
                        {
                            "stage": "collect_evidence",
                            "code": "EMPTY_EVIDENCE",
                            "message": ("collector returned no evidence"),
                        }
                    ],
                    "trace": [
                        trace_event(
                            "collect_evidence",
                            "failed",
                            "no evidence was collected",
                        )
                    ],
                }

            normalized_errors = [
                {
                    "stage": "collect_evidence",
                    "code": "KUBERNETES_COLLECTION_PARTIAL_ERROR",
                    "operation": error.get("operation"),
                    "resource_kind": error.get("resource_kind"),
                    "resource_name": error.get("resource_name"),
                    "status_code": error.get("status_code"),
                    "message": error.get("message", str(error)),
                }
                if isinstance(error, dict)
                else {
                    "stage": "collect_evidence",
                    "code": "KUBERNETES_COLLECTION_PARTIAL_ERROR",
                    "message": str(error),
                }
                for error in collector_errors
            ]

            return {
                "phase": (
                    "evidence_collected_with_errors"
                    if normalized_errors
                    else "evidence_collected"
                ),
                "evidence": evidence,
                "error_count": (
                    state.get("error_count", 0)
                    + len(normalized_errors)
                ),
                "errors": normalized_errors,
                "trace": [
                    trace_event(
                        "collect_evidence",
                        "completed",
                        (
                            f"collected {len(evidence)} evidence items; "
                            f"collector errors: {len(normalized_errors)}"
                        ),
                    )
                ],
            }

        except Exception as exc:
            return {
                "phase": "evidence_collection_failed",
                "error_count": (state.get("error_count", 0) + 1),
                "errors": [
                    {
                        "stage": "collect_evidence",
                        "code": "COLLECTION_ERROR",
                        "message": str(exc),
                    }
                ],
                "trace": [
                    trace_event(
                        "collect_evidence",
                        "failed",
                        "evidence collection raised an exception",
                    )
                ],
            }

    return collect_evidence

def make_retrieve_runbooks_node(
    retriever: RunbookRetrieverPort,
) -> Callable[[IncidentState], dict[str, Any]]:
    def retrieve_runbooks(
        state: IncidentState,
    ) -> dict[str, Any]:
        try:
            query = build_retrieval_query(state)
            runbooks = retriever.retrieve(
                query=query,
                k=3,
            )

            if not runbooks:
                return {
                    "phase": "runbook_retrieval_failed",
                    "retrieval_query": query,
                    "error_count": (
                        state.get("error_count", 0) + 1
                    ),
                    "errors": [
                        {
                            "stage": "retrieve_runbooks",
                            "code": "NO_RUNBOOK_FOUND",
                            "message": (
                                "retriever returned no runbooks"
                            ),
                        }
                    ],
                    "trace": [
                        trace_event(
                            "retrieve_runbooks",
                            "failed",
                            "no runbook was retrieved",
                        )
                    ],
                }

            return {
                "phase": "runbooks_retrieved",
                "retrieval_query": query,
                "retrieved_runbooks": runbooks,
                "trace": [
                    trace_event(
                        "retrieve_runbooks",
                        "completed",
                        (
                            f"retrieved {len(runbooks)} "
                            "runbook chunks"
                        ),
                    )
                ],
            }

        except Exception as exc:
            return {
                "phase": "runbook_retrieval_failed",
                "error_count": (
                    state.get("error_count", 0) + 1
                ),
                "errors": [
                    {
                        "stage": "retrieve_runbooks",
                        "code": "RETRIEVAL_ERROR",
                        "message": str(exc),
                    }
                ],
                "trace": [
                    trace_event(
                        "retrieve_runbooks",
                        "failed",
                        "runbook retrieval raised an exception",
                    )
                ],
            }

    return retrieve_runbooks

def validate_diagnosis_references(
    *,
    diagnosis: Diagnosis,
    state: IncidentState,
) -> None:
    available_evidence_ids = {
        item["evidence_id"]
        for item in state.get("evidence", [])
        if item.get("evidence_id")
    }

    available_runbook_ids = {
        item["runbook_id"]
        for item in state.get(
            "retrieved_runbooks",
            [],
        )
        if item.get("runbook_id")
    }

    invalid_evidence_ids = (
        set(diagnosis.evidence_ids)
        - available_evidence_ids
    )

    invalid_runbook_ids = (
        set(diagnosis.runbook_ids)
        - available_runbook_ids
    )

    if invalid_evidence_ids:
        raise InvalidDiagnosisReference(
            "unknown evidence IDs: "
            f"{sorted(invalid_evidence_ids)!r}"
        )

    if invalid_runbook_ids:
        raise InvalidDiagnosisReference(
            "unknown runbook IDs: "
            f"{sorted(invalid_runbook_ids)!r}"
        )

    mentioned_evidence_ids = set(
        EVIDENCE_ID_PATTERN.findall(
            diagnosis.root_cause
            + "\n"
            + diagnosis.reasoning_summary
        )
    )

    unlisted_evidence_ids = (
        mentioned_evidence_ids
        - set(diagnosis.evidence_ids)
    )

    if unlisted_evidence_ids:
        raise InvalidDiagnosisReference(
            "evidence IDs mentioned in diagnosis "
            "but missing from evidence_ids: "
            f"{sorted(unlisted_evidence_ids)!r}"
        )

    if (
        diagnosis.fault_category
        not in {"unknown", "no_fault_detected"}
        and not diagnosis.runbook_ids
    ):
        raise InvalidDiagnosisReference(
            "fault diagnosis must reference "
            "at least one retrieved runbook"
        )

def make_diagnose_incident_node(
    diagnoser: DiagnosisServicePort,
) -> Callable[[IncidentState], dict[str, Any]]:
    def diagnose_incident(
        state: IncidentState,
    ) -> dict[str, Any]:
        try:
            result = diagnoser.diagnose(state)

            diagnosis_normalized = result.diagnosis.model_copy(
                update={
                    "evidence_ids": _normalize_reference_ids(
                        result.diagnosis.evidence_ids
                    ),
                    "runbook_ids": _normalize_reference_ids(
                        result.diagnosis.runbook_ids
                    ),
                }
            )

            validate_diagnosis_references(
                diagnosis=diagnosis_normalized,
                state=state,
            )

            return {
                "phase": "diagnosis_completed",
                "diagnosis": (
                    diagnosis_normalized.model_dump()
                ),
                "llm_model": result.model_name,
                "llm_usage": result.usage,
                "trace": [
                    trace_event(
                        "diagnose_incident",
                        "completed",
                        (
                            "structured diagnosis "
                            "completed"
                        ),
                    )
                ],
            }

        except InvalidDiagnosisReference as exc:
            return {
                "phase": "diagnosis_failed",
                "error_count": (
                    state.get("error_count", 0) + 1
                ),
                "errors": [
                    {
                        "stage": "diagnose_incident",
                        "code": (
                            "INVALID_DIAGNOSIS_REFERENCE"
                        ),
                        "message": str(exc),
                    }
                ],
                "trace": [
                    trace_event(
                        "diagnose_incident",
                        "failed",
                        "diagnosis reference validation failed",
                    )
                ],
            }

        except Exception as exc:
            return {
                "phase": "diagnosis_failed",
                "error_count": (
                    state.get("error_count", 0) + 1
                ),
                "errors": [
                    {
                        "stage": "diagnose_incident",
                        "code": "LLM_DIAGNOSIS_ERROR",
                        "message": str(exc),
                    }
                ],
                "trace": [
                    trace_event(
                        "diagnose_incident",
                        "failed",
                        "LLM diagnosis failed",
                    )
                ],
            }

    return diagnose_incident
