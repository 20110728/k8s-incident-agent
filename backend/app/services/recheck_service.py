"""Fresh, read-only cluster observations; never resume the incident workflow.

History is separate from approval/checkpoint state. A current healthy observation
does not establish that an earlier Agent action caused the recovery.
"""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from backend.app.agent.business_recovery import UNVERIFIED_SCOPE, recovery_resource_view
from backend.app.agent.collector_adapter import normalize_evidence
from backend.app.agent.diagnosis_policy import diagnostic_facts
from backend.app.agent.schemas import IncidentRequest


class RecheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=2000)

    @field_validator("note")
    @classmethod
    def strip_note(cls, value):
        return value.strip()


class RecheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recheck_id: str
    incident_id: str
    started_at: str
    finished_at: str
    note: str
    note_source: Literal["user_supplied_unverified"] = "user_supplied_unverified"
    status: Literal["passed", "failed", "unknown"]
    resource_status: Literal["ready", "not_ready", "unknown"]
    business_status: Literal["passed", "failed", "unknown"]
    target_comparison: dict[str, Any]
    recovery_attribution: Literal["not_established"] = "not_established"
    evidence: list[dict[str, Any]]
    service_profile: dict[str, Any]
    policy_facts: dict[str, Any]
    collection_errors: list[dict[str, Any]]
    unverified_scope: list[str]
    excluded_terminating_pods: list[str]
    error_code: str | None = None
    cluster_writes_executed: Literal[False] = False
    model_called: Literal[False] = False


class RecheckUnavailable(ValueError):
    pass


TERMINAL_PHASES = {
    "diagnosis_failed",
    "remediation_failed",
    "remediation_skipped",
    "approval_rejected",
    "approval_failed",
    "remediation_execution_failed",
    "remediation_execution_conflict",
    "verification_failed",
    "verification_succeeded",
    "verification_skipped",
    "failed",
}


def compare_targets(original, current):
    # Only compare fields actually captured by both observations. Missing identity
    # is unknown, not proof that the target stayed unchanged.
    fields = ("digest", "deployment_uid", "deployment_generation")
    changes = {
        key: {"before": original.get(key), "after": current.get(key)}
        for key in fields
        if original.get(key) is not None
        and current.get(key) is not None
        and original[key] != current[key]
    }
    complete = original.get("status") == current.get("status") == "matched" and all(
        original.get(key) is not None and current.get(key) is not None for key in fields
    )
    return {
        "status": (
            "changed" if changes else "same_observed_fields" if complete else "unknown"
        ),
        "compared_fields": list(fields),
        "changes": changes,
        "original_profile_status": original.get("status"),
        "current_profile_status": current.get("status"),
    }


class IncidentRecheckService:
    def __init__(self, incidents, collector, repository):
        self.incidents = incidents
        self.collector = collector
        self.repository = repository

    def create(self, incident_id: str, request: RecheckRequest) -> RecheckResult:
        # Read the completed checkpoint, but never invoke/update the graph.
        # The copy keeps lifecycle filtering isolated from approval evidence.
        snapshot = self.incidents.get_incident(incident_id)
        original = deepcopy(snapshot.state)
        phase = original.get("phase")
        manual = (
            phase == "remediation_planned"
            and (original.get("remediation_plan") or {}).get("action")
            == "manual_investigation"
            and original.get("requires_approval") is not True
        )
        if not (phase in TERMINAL_PHASES or manual) or snapshot.waiting_for_approval:
            raise RecheckUnavailable(
                "Recheck requires a completed incident; pending approval or active workflows are excluded."
            )
        try:
            target = IncidentRequest.model_validate(original.get("request"))
        except ValidationError as error:
            raise RecheckUnavailable(
                "Original incident has no valid target request."
            ) from error
        if target.namespace != "agent-demo":
            raise RecheckUnavailable(
                "Incident target is outside the permitted namespace."
            )
        recheck_id = str(uuid4())
        # Each POST creates an independent observation, including when the user
        # repeats the same note. This endpoint is not an idempotent write retry.
        started = datetime.now(UTC).isoformat()
        evidence, profile, errors, excluded = [], {}, [], []
        facts = diagnostic_facts({})
        error_code = None
        status = "unknown"
        scope = [
            *UNVERIFIED_SCOPE,
            "恢复与先前 Agent 操作或人工说明之间的因果关系",
            "Service/Pod 的完整 UID 身份比较",
        ]
        try:
            bundle = self.collector.collect(target.namespace, target.service_name)
            if (bundle.get("namespace"), bundle.get("service_name")) != (
                target.namespace,
                target.service_name,
            ):
                raise ValueError("RECHECK_TARGET_MISMATCH")
            evidence = normalize_evidence(incident_id=recheck_id, bundle=bundle)
            profile = bundle.get("service_profile") or {}
            errors = bundle.get("errors", [])
            observed = dict(
                request=target.model_dump(), service_profile=profile, evidence=evidence
            )
            # Reuse the conservative lifecycle view; persist the unfiltered
            # evidence below so excluded Pods remain visible for audit.
            view, excluded = recovery_resource_view(observed)
            facts = diagnostic_facts(view)
            if excluded:
                scope.append("退出中 Pod 的最终删除与清理：" + ", ".join(excluded))
            healthy = (
                facts["resource_status"] == "ready"
                and facts["business_status"] == "passed"
                and facts["readiness_configuration_status"] == "matched"
                and not facts["selector_drift"]
                and not facts["current_runtime_faults"]
            )
            failed = (
                facts["resource_status"] == "not_ready"
                or facts["business_status"] == "failed"
                or facts["selector_drift"]
                or facts["readiness_drift"]
                or facts["current_runtime_faults"]
            )
            status = "passed" if healthy else "failed" if failed else "unknown"
        except Exception as error:
            # Keep any collected evidence, but do not reuse an earlier success.
            error_code = "RECHECK_OBSERVATION_INVALID"
            errors = [
                *errors,
                {
                    "operation": "recheck",
                    "error_type": type(error).__name__,
                    "message": "Fresh observation could not be validated.",
                },
            ]
            facts = diagnostic_facts({})
        result = RecheckResult(
            recheck_id=recheck_id,
            incident_id=incident_id,
            started_at=started,
            finished_at=datetime.now(UTC).isoformat(),
            note=request.note,
            status=status,
            resource_status=facts["resource_status"],
            business_status=facts["business_status"],
            target_comparison=compare_targets(
                original.get("service_profile") or {}, profile
            ),
            evidence=evidence,
            service_profile=profile,
            policy_facts=facts,
            collection_errors=errors,
            unverified_scope=scope,
            excluded_terminating_pods=excluded,
            error_code=error_code,
        )
        # Failure to persist must fail the request; never claim a saved observation.
        self.repository.append(result)
        return result

    def history(self, incident_id: str, limit=20, before_sequence=None):
        # Verify incident existence, then read only the separate history table.
        # History refresh must not create a new sample or initialize cluster I/O.
        self.incidents.get_incident(incident_id)
        return self.repository.list(incident_id, limit, before_sequence)
