from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from pydantic import ValidationError

from backend.app.agent.remediation_policy import SAFE_NAMESPACE
from backend.app.agent.schemas import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    RemediationPlan,
)
from backend.app.agent.state import IncidentState


class InvalidApprovalRequest(ValueError):
    """Raised when an approval request cannot be built safely."""


class InvalidApprovalDecision(ValueError):
    """Raised when a human approval decision is malformed or mismatched."""


def build_approval_request(state: IncidentState) -> ApprovalRequest:
    incident_id = str(state.get("incident_id") or "").strip()
    if not incident_id:
        raise InvalidApprovalRequest("incident_id is required")

    raw_plan = state.get("remediation_plan")
    if raw_plan is None:
        raise InvalidApprovalRequest("remediation plan is required")

    try:
        plan = RemediationPlan.model_validate(raw_plan)
    except ValidationError as exc:
        raise InvalidApprovalRequest(
            "remediation plan is invalid"
        ) from exc

    if state.get("requires_approval") is not True:
        raise InvalidApprovalRequest(
            "state does not require approval"
        )

    if plan.requires_approval is not True:
        raise InvalidApprovalRequest(
            "remediation plan does not require approval"
        )

    if plan.action == "manual_investigation":
        raise InvalidApprovalRequest(
            "manual investigation must not require approval"
        )

    if plan.parameters.namespace != SAFE_NAMESPACE:
        raise InvalidApprovalRequest(
            f"approval target namespace must be {SAFE_NAMESPACE}"
        )

    fingerprint_payload = {
        "incident_id": incident_id,
        "remediation_plan": plan.model_dump(mode="json"),
    }
    canonical_payload = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(
        canonical_payload.encode("utf-8")
    ).hexdigest()[:16]

    return ApprovalRequest(
        approval_id=f"apr-{digest}",
        incident_id=incident_id,
        plan=plan,
    )


def validate_approval_decision(
    value: Any,
    expected_request: ApprovalRequest,
) -> ApprovalDecision:
    try:
        decision = ApprovalDecision.model_validate(value)
    except ValidationError as exc:
        raise InvalidApprovalDecision(
            "approval decision is invalid"
        ) from exc

    if decision.approval_id != expected_request.approval_id:
        raise InvalidApprovalDecision(
            "approval decision does not match the pending approval request"
        )

    return decision


def create_approval_record(
    request: ApprovalRequest,
    decision: ApprovalDecision,
) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=request.approval_id,
        incident_id=request.incident_id,
        action=request.plan.action,
        approved=decision.approved,
        approver=decision.approver,
        comment=decision.comment,
        decided_at=datetime.now(timezone.utc).isoformat(),
    )


def load_existing_approval_record(
    state: Mapping[str, Any],
) -> ApprovalRecord | None:
    raw_record = state.get("approval_record")
    if raw_record is None:
        return None

    try:
        return ApprovalRecord.model_validate(raw_record)
    except ValidationError as exc:
        raise InvalidApprovalDecision(
            "existing approval record is invalid"
        ) from exc