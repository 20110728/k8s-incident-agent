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

from backend.app.agent.remediation_policy import (
    InvalidRemediationPlan,
    validate_remediation_plan,
)

from backend.app.llm.remediation_planner import (
    RemediationPlannerPort,
)

from langgraph.types import interrupt

from backend.app.agent.approval import (
    InvalidApprovalDecision,
    InvalidApprovalRequest,
    build_approval_request,
    create_approval_record,
    load_existing_approval_record,
    validate_approval_decision,
)
from backend.app.agent.schemas import ApprovalRequest

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
        accumulated_usage: dict[str, int] = {}
        retry_count = 0

        try:
            validation_feedback: str | None = None

            # 首次调用加一次校验重试，
            # 最多进行两次LLM诊断。
            for attempt_index in range(2):
                call_state = dict(state)

                if validation_feedback is not None:
                    call_state[
                        "diagnosis_validation_feedback"
                    ] = validation_feedback
                    retry_count = 1

                result = diagnoser.diagnose(
                    call_state
                )

                for key, value in (
                    result.usage.items()
                ):
                    if not isinstance(value, int):
                        continue

                    accumulated_usage[key] = (
                        accumulated_usage.get(
                            key,
                            0,
                        )
                        + value
                    )

                try:
                    diagnosis_normalized = (
                        result.diagnosis.model_copy(
                            update={
                                "evidence_ids": (
                                    _normalize_reference_ids(
                                        result.diagnosis.evidence_ids
                                    )
                                ),
                                "runbook_ids": (
                                    _normalize_reference_ids(
                                        result.diagnosis.runbook_ids
                                    )
                                ),
                            }
                        )
                    )

                    validate_diagnosis_references(
                        diagnosis=diagnosis_normalized,
                        state=state,
                    )

                except InvalidDiagnosisReference as exc:
                    if attempt_index == 0:
                        validation_feedback = str(exc)
                        continue

                    raise

                trace_message = (
                    "structured diagnosis completed"
                )

                if retry_count:
                    trace_message = (
                        "structured diagnosis completed "
                        "after one validation retry"
                    )

                return {
                    "phase": "diagnosis_completed",
                    "diagnosis": (
                        diagnosis_normalized.model_dump()
                    ),
                    "llm_model": result.model_name,
                    "llm_usage": accumulated_usage,
                    "diagnosis_retry_count": (
                        retry_count
                    ),
                    "trace": [
                        trace_event(
                            "diagnose_incident",
                            "completed",
                            trace_message,
                        )
                    ],
                }

            raise RuntimeError(
                "diagnosis retry loop ended "
                "without a result"
            )

        except InvalidDiagnosisReference as exc:
            return {
                "phase": "diagnosis_failed",
                "diagnosis_retry_count": (
                    retry_count
                ),
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
                        (
                            "diagnosis reference "
                            "validation failed"
                        ),
                    )
                ],
            }

        except Exception as exc:
            return {
                "phase": "diagnosis_failed",
                "diagnosis_retry_count": (
                    retry_count
                ),
                "error_count": (
                    state.get("error_count", 0) + 1
                ),
                "errors": [
                    {
                        "stage": "diagnose_incident",
                        "code": (
                            "LLM_DIAGNOSIS_ERROR"
                        ),
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

def skip_remediation(
    state: IncidentState,
) -> dict[str, Any]:
    diagnosis = state.get("diagnosis") or {}
    fault_category = diagnosis.get(
        "fault_category",
        "unknown",
    )

    return {
        "phase": "remediation_skipped",
        "remediation_plan": None,
        "risk_level": None,
        "requires_approval": False,
        "approved": None,
        "trace": [
            trace_event(
                "skip_remediation",
                "completed",
                (
                    "remediation skipped for "
                    f"fault category {fault_category}"
                ),
            )
        ],
    }


def make_plan_remediation_node(
    planner: RemediationPlannerPort,
) -> Callable[[IncidentState], dict[str, Any]]:
    def plan_remediation(
        state: IncidentState,
    ) -> dict[str, Any]:
        try:
            result = planner.plan(state)

            validated_plan = (
                validate_remediation_plan(
                    plan=result.plan,
                    state=state,
                )
            )

            return {
                "phase": "remediation_planned",
                "remediation_plan": (
                    validated_plan.model_dump()
                ),
                "risk_level": (
                    validated_plan.risk_level
                ),
                "requires_approval": (
                    validated_plan.requires_approval
                ),
                "approved": None,
                "remediation_llm_model": (
                    result.model_name
                ),
                "remediation_llm_usage": (
                    result.usage
                ),
                "trace": [
                    trace_event(
                        "plan_remediation",
                        "completed",
                        (
                            "structured remediation "
                            "plan completed"
                        ),
                    )
                ],
            }

        except InvalidRemediationPlan as exc:
            return {
                "phase": "remediation_failed",
                "error_count": (
                    state.get("error_count", 0) + 1
                ),
                "errors": [
                    {
                        "stage": "plan_remediation",
                        "code": (
                            "INVALID_REMEDIATION_PLAN"
                        ),
                        "message": str(exc),
                    }
                ],
                "trace": [
                    trace_event(
                        "plan_remediation",
                        "failed",
                        (
                            "remediation plan "
                            "validation failed"
                        ),
                    )
                ],
            }

        except Exception as exc:
            return {
                "phase": "remediation_failed",
                "error_count": (
                    state.get("error_count", 0) + 1
                ),
                "errors": [
                    {
                        "stage": "plan_remediation",
                        "code": (
                            "LLM_REMEDIATION_ERROR"
                        ),
                        "message": str(exc),
                    }
                ],
                "trace": [
                    trace_event(
                        "plan_remediation",
                        "failed",
                        (
                            "LLM remediation "
                            "planning failed"
                        ),
                    )
                ],
            }

    return plan_remediation

def prepare_approval(state: IncidentState) -> dict[str, Any]:
    """Create and persist the deterministic approval request."""

    if state.get("requires_approval") is not True:
        return {
            "phase": "remediation_planned",
            "approval_status": "not_required",
            "approved": None,
            "trace": [
                trace_event(
                    step="prepare_approval",
                    status="completed",
                    message="remediation plan does not require approval",
                )
            ],
        }

    try:
        existing_record = load_existing_approval_record(state)
        if existing_record is not None:
            status = (
                "approved"
                if existing_record.approved
                else "rejected"
            )
            return {
                "phase": f"approval_{status}",
                "approval_status": status,
                "approved": existing_record.approved,
                "trace": [
                    trace_event(
                        step="prepare_approval",
                        status="completed",
                        message="existing approval record preserved",
                    )
                ],
            }

        approval_request = build_approval_request(state)
    except (InvalidApprovalRequest, InvalidApprovalDecision) as exc:
        return {
            "phase": "approval_failed",
            "approval_status": "failed",
            "approved": None,
            "errors": [
                {
                    "stage": "prepare_approval",
                    "code": "INVALID_APPROVAL_REQUEST",
                    "message": str(exc),
                }
            ],
            "trace": [
                trace_event(
                    step="prepare_approval",
                    status="failed",
                    message="approval request validation failed",
                )
            ],
        }

    return {
        "phase": "awaiting_approval",
        "approval_status": "pending",
        "approval_request": approval_request,
        "approved": None,
        "trace": [
            trace_event(
                step="prepare_approval",
                status="completed",
                message="approval request prepared",
            )
        ],
    }


def request_human_approval(
    state: IncidentState,
) -> dict[str, Any]:
    """Pause the graph and record one human approval decision."""

    try:
        existing_record = load_existing_approval_record(state)
    except InvalidApprovalDecision as exc:
        return {
            "phase": "approval_failed",
            "approval_status": "failed",
            "errors": [
                {
                    "stage": "request_human_approval",
                    "code": "INVALID_APPROVAL_RECORD",
                    "message": str(exc),
                }
            ],
            "trace": [
                trace_event(
                    step="request_human_approval",
                    status="failed",
                    message="existing approval record is invalid",
                )
            ],
        }

    if existing_record is not None:
        status = (
            "approved"
            if existing_record.approved
            else "rejected"
        )
        return {
            "phase": f"approval_{status}",
            "approval_status": status,
            "approved": existing_record.approved,
            "trace": [
                trace_event(
                    step="request_human_approval",
                    status="completed",
                    message="duplicate approval request ignored",
                )
            ],
        }

    try:
        expected_request = build_approval_request(state)
        stored_request = ApprovalRequest.model_validate(
            state.get("approval_request")
        )

        if stored_request != expected_request:
            raise InvalidApprovalRequest(
                "stored approval request does not match remediation plan"
            )
    except Exception as exc:
        return {
            "phase": "approval_failed",
            "approval_status": "failed",
            "approved": None,
            "errors": [
                {
                    "stage": "request_human_approval",
                    "code": "INVALID_APPROVAL_REQUEST",
                    "message": str(exc),
                }
            ],
            "trace": [
                trace_event(
                    step="request_human_approval",
                    status="failed",
                    message="approval request validation failed",
                )
            ],
        }

    resume_value = interrupt(
        {
            "type": "remediation_approval_required",
            "message": "该处置方案需要人工审批。",
            "approval_request": expected_request.model_dump(
                mode="json"
            ),
        }
    )

    try:
        decision = validate_approval_decision(
            resume_value,
            expected_request,
        )
    except InvalidApprovalDecision as exc:
        return {
            "phase": "approval_failed",
            "approval_status": "failed",
            "approved": None,
            "errors": [
                {
                    "stage": "request_human_approval",
                    "code": "INVALID_APPROVAL_DECISION",
                    "message": str(exc),
                }
            ],
            "trace": [
                trace_event(
                    step="request_human_approval",
                    status="failed",
                    message="approval decision validation failed",
                )
            ],
        }

    record = create_approval_record(
        expected_request,
        decision,
    )
    status = "approved" if decision.approved else "rejected"

    return {
        "phase": f"approval_{status}",
        "approval_status": status,
        "approval_request": expected_request,
        "approval_record": record,
        "approved": decision.approved,
        "trace": [
            trace_event(
                step="request_human_approval",
                status="completed",
                message=f"remediation plan {status} by human",
            )
        ],
    }