from __future__ import annotations

import hashlib
import re

from dataclasses import dataclass

from pydantic import ValidationError

from backend.app.agent.approval import (
    InvalidApprovalRequest,
    build_approval_request,
)
from backend.app.agent.remediation_policy import (
    EXECUTABLE_REMEDIATION_ACTIONS,
    InvalidRemediationPlan,
    validate_remediation_plan,
)
from backend.app.agent.schemas import (
    ActionExecutionResult,
    ApprovalRecord,
    ApprovalRequest,
    RemediationPlan,
)
from backend.app.agent.state import IncidentState


APPROVAL_ID_PATTERN = re.compile(
    r"^apr-[0-9a-f]{16}$"
)

COMPLETED_EXECUTION_STATUSES = frozenset(
    {
        "succeeded",
        "already_applied",
    }
)


class InvalidExecutionAuthorization(ValueError):
    """Raised when a write operation is not authorized."""


@dataclass(frozen=True)
class ExecutionAuthorization:
    execution_id: str
    approval_id: str
    incident_id: str
    plan: RemediationPlan
    approval_request: ApprovalRequest
    approval_record: ApprovalRecord
    previous_result: ActionExecutionResult | None
    already_executed: bool


def build_execution_id(
    approval_id: str,
) -> str:
    normalized = approval_id.strip()

    if APPROVAL_ID_PATTERN.fullmatch(
        normalized
    ) is None:
        raise InvalidExecutionAuthorization(
            "approval_id has an invalid format"
        )

    digest = hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()[:16]

    return f"exec-{digest}"


def _load_plan(
    state: IncidentState,
) -> RemediationPlan:
    raw_plan = state.get("remediation_plan")

    if raw_plan is None:
        raise InvalidExecutionAuthorization(
            "remediation plan is required"
        )

    try:
        return RemediationPlan.model_validate(
            raw_plan
        )
    except ValidationError as exc:
        raise InvalidExecutionAuthorization(
            "remediation plan is invalid"
        ) from exc


def _load_approval_request(
    state: IncidentState,
) -> ApprovalRequest:
    raw_request = state.get("approval_request")

    if raw_request is None:
        raise InvalidExecutionAuthorization(
            "approval request is required"
        )

    try:
        return ApprovalRequest.model_validate(
            raw_request
        )
    except ValidationError as exc:
        raise InvalidExecutionAuthorization(
            "approval request is invalid"
        ) from exc


def _load_approval_record(
    state: IncidentState,
) -> ApprovalRecord:
    raw_record = state.get("approval_record")

    if raw_record is None:
        raise InvalidExecutionAuthorization(
            "approval record is required"
        )

    try:
        return ApprovalRecord.model_validate(
            raw_record
        )
    except ValidationError as exc:
        raise InvalidExecutionAuthorization(
            "approval record is invalid"
        ) from exc


def _load_previous_result(
    state: IncidentState,
) -> ActionExecutionResult | None:
    raw_result = state.get("action_result")

    if raw_result is None:
        return None

    try:
        return ActionExecutionResult.model_validate(
            raw_result
        )
    except ValidationError as exc:
        raise InvalidExecutionAuthorization(
            "existing action result is invalid"
        ) from exc


def _validate_approved_state(
    state: IncidentState,
) -> None:
    if state.get("phase") != "approval_approved":
        raise InvalidExecutionAuthorization(
            "workflow phase is not approval_approved"
        )

    if state.get("requires_approval") is not True:
        raise InvalidExecutionAuthorization(
            "write operation does not have an "
            "approval requirement"
        )

    if state.get("approval_status") != "approved":
        raise InvalidExecutionAuthorization(
            "approval status is not approved"
        )

    if state.get("approved") is not True:
        raise InvalidExecutionAuthorization(
            "write operation was not approved"
        )


def _validate_action_is_executable(
    plan: RemediationPlan,
) -> None:
    if (
        plan.action
        not in EXECUTABLE_REMEDIATION_ACTIONS
    ):
        raise InvalidExecutionAuthorization(
            f"action {plan.action!r} is not executable"
        )


def _validate_approval_binding(
    *,
    state: IncidentState,
    plan: RemediationPlan,
    approval_request: ApprovalRequest,
    approval_record: ApprovalRecord,
) -> None:
    incident_id = str(
        state.get("incident_id") or ""
    ).strip()

    if not incident_id:
        raise InvalidExecutionAuthorization(
            "incident_id is required"
        )

    if approval_request.incident_id != incident_id:
        raise InvalidExecutionAuthorization(
            "approval request incident_id does not "
            "match workflow state"
        )

    if approval_record.incident_id != incident_id:
        raise InvalidExecutionAuthorization(
            "approval record incident_id does not "
            "match workflow state"
        )

    if (
        approval_request.approval_id
        != approval_record.approval_id
    ):
        raise InvalidExecutionAuthorization(
            "approval IDs do not match"
        )

    if approval_record.approved is not True:
        raise InvalidExecutionAuthorization(
            "approval record does not approve "
            "the write operation"
        )

    if approval_request.plan != plan:
        raise InvalidExecutionAuthorization(
            "remediation plan changed after approval"
        )

    if approval_record.action != plan.action:
        raise InvalidExecutionAuthorization(
            "approval record action does not match "
            "the remediation plan"
        )

    try:
        expected_request = build_approval_request(
            state
        )
    except InvalidApprovalRequest as exc:
        raise InvalidExecutionAuthorization(
            "approval request cannot be rebuilt "
            "from workflow state"
        ) from exc

    if expected_request != approval_request:
        raise InvalidExecutionAuthorization(
            "approval request does not match "
            "the approved remediation plan"
        )


def _revalidate_plan(
    *,
    state: IncidentState,
    plan: RemediationPlan,
) -> None:
    try:
        validate_remediation_plan(
            plan=plan,
            state=state,
        )
    except InvalidRemediationPlan as exc:
        raise InvalidExecutionAuthorization(
            "remediation plan failed "
            "execution-time validation: "
            f"{exc}"
        ) from exc


def _validate_previous_result(
    *,
    previous_result: ActionExecutionResult,
    execution_id: str,
    approval_request: ApprovalRequest,
    plan: RemediationPlan,
) -> bool:
    if previous_result.execution_id != execution_id:
        raise InvalidExecutionAuthorization(
            "existing action result belongs to "
            "another execution"
        )

    if (
        previous_result.approval_id
        != approval_request.approval_id
    ):
        raise InvalidExecutionAuthorization(
            "existing action result belongs to "
            "another approval"
        )

    if previous_result.action != plan.action:
        raise InvalidExecutionAuthorization(
            "existing action result belongs to "
            "another remediation action"
        )

    if (
        previous_result.namespace
        != plan.parameters.namespace
        or previous_result.resource_kind
        != plan.parameters.resource_kind
        or previous_result.resource_name
        != plan.parameters.resource_name
    ):
        raise InvalidExecutionAuthorization(
            "existing action result belongs to "
            "another remediation target"
        )

    if (
        previous_result.status
        in COMPLETED_EXECUTION_STATUSES
    ):
        return True

    raise InvalidExecutionAuthorization(
        "an execution attempt has already been "
        "recorded for this approval"
    )


def validate_execution_authorization(
    state: IncidentState,
) -> ExecutionAuthorization:
    _validate_approved_state(state)

    plan = _load_plan(state)
    approval_request = (
        _load_approval_request(state)
    )
    approval_record = _load_approval_record(
        state
    )

    _validate_action_is_executable(plan)

    _validate_approval_binding(
        state=state,
        plan=plan,
        approval_request=approval_request,
        approval_record=approval_record,
    )

    _revalidate_plan(
        state=state,
        plan=plan,
    )

    execution_id = build_execution_id(
        approval_request.approval_id
    )
    previous_result = _load_previous_result(
        state
    )

    already_executed = False

    if previous_result is not None:
        already_executed = (
            _validate_previous_result(
                previous_result=previous_result,
                execution_id=execution_id,
                approval_request=approval_request,
                plan=plan,
            )
        )

    return ExecutionAuthorization(
        execution_id=execution_id,
        approval_id=approval_request.approval_id,
        incident_id=approval_request.incident_id,
        plan=plan,
        approval_request=approval_request,
        approval_record=approval_record,
        previous_result=previous_result,
        already_executed=already_executed,
    )