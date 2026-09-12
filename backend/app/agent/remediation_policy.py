# 处置策略边界：由实际证据与版本匹配的配置决定可建议动作，并校验计划参数。
# 写操作仅限登记的 selector 和 readiness 修正，不能用放宽探针掩盖业务失败。

from pydantic import ValidationError
from backend.app.agent.schemas import Diagnosis
from backend.app.agent.diagnosis_policy import (
    diagnostic_facts, validate_diagnosis_assessment, InvalidDiagnosisAssessment,
)

import re
from typing import Any

from backend.app.agent.schemas import (
    LabelPair,
    RemediationPlan,
)
from backend.app.agent.state import IncidentState
from backend.app.service_profiles.registry import (
    ProfileUnavailable, matched_profile, validate_profile_action,
)

SAFE_NAMESPACE = "agent-demo"

ALLOWED_REMEDIATION_ACTIONS = frozenset(
    {
        "manual_investigation",
        "patch_readiness_probe",
        "patch_service_selector",
    }
)

EXECUTABLE_REMEDIATION_ACTIONS = frozenset(
    {
        "patch_readiness_probe",
        "patch_service_selector",
    }
)

ALLOWED_ACTIONS_BY_FAULT_CATEGORY = {
    "application_error": {"manual_investigation"},
    "dependency_error": {"manual_investigation"},
    "crash_loop_backoff": {
        "manual_investigation",
    },
    "image_pull_backoff": {
        "manual_investigation",
    },
    "oom_killed": {
        "manual_investigation",
    },
    "readiness_probe_error": {
        "manual_investigation",
        "patch_readiness_probe",
    },
    "service_selector_mismatch": {
        "manual_investigation",
        "patch_service_selector",
    },
}

RESOURCE_EVIDENCE_TYPES = {
    "Service": "Service",
    "Deployment": "Deployment",
    "Pod": "PodStatus",
}

FORBIDDEN_COMMAND_PATTERN = re.compile(
    (
        r"(`{3,}|\$\(|"
        r"\b(?:"
        r"kubectl|helm|bash|sh|curl|wget|docker"
        r")\b)"
    ),
    flags=re.IGNORECASE | re.ASCII,
)


class InvalidRemediationPlan(ValueError):
    pass


def _label_pairs_to_dict(
    pairs: list[LabelPair],
) -> dict[str, str]:
    result: dict[str, str] = {}

    for pair in pairs:
        if pair.key in result:
            raise InvalidRemediationPlan(
                "selector contains duplicate label keys"
            )

        result[pair.key] = pair.value

    return result


def _find_evidence(
    state: IncidentState,
    *,
    resource_type: str,
    resource_name: str,
) -> list[dict[str, Any]]:
    return [
        item
        for item in state.get("evidence", [])
        if (
            item.get("resource_type")
            == resource_type
            and item.get("resource_name")
            == resource_name
        )
    ]


def _has_grounded_readiness_candidate(state: IncidentState) -> bool:
    try:
        profile = matched_profile(state)
    except ProfileUnavailable:
        return False
    deployments = _find_evidence(state, resource_type="Deployment",
                                 resource_name=profile.deployment_name)
    for container in deployments[0]["data"].get("containers", []):
        if container.get("name") != profile.container_name:
            continue
        probe = container.get("readiness_probe") or {}
        expected = profile.readiness_probe
        # This executor changes path/port only. Scheme changes require manual work.
        return bool(probe.get("path") and probe.get("port") is not None
                    and (probe.get("scheme") or "HTTP") == expected.scheme
                    and (probe.get("path"), probe.get("port")) != (expected.path, expected.port))
    return False


def _has_grounded_selector_candidate(state: IncidentState) -> bool:
    try:
        profile = matched_profile(state)
    except ProfileUnavailable:
        return False
    services = _find_evidence(state, resource_type="Service",
                             resource_name=profile.service_name)
    return (len(services) == 1
            and isinstance(services[0].get("data", {}).get("selector"), dict)
            and services[0]["data"]["selector"] != profile.expected_selector)


def get_allowed_remediation_actions(
    state: IncidentState,
) -> set[str]:
    diagnosis = state.get("diagnosis") or {}
    fault_category = diagnosis.get(
        "fault_category"
    )

    actions = set(
        ALLOWED_ACTIONS_BY_FAULT_CATEGORY.get(
            fault_category,
            set(),
        )
    )

    if fault_category == "readiness_probe_error":
        if not _has_grounded_readiness_candidate(
            state
        ):
            actions.discard(
                "patch_readiness_probe"
            )

    if fault_category == (
        "service_selector_mismatch"
    ):
        if not _has_grounded_selector_candidate(
            state
        ):
            actions.discard(
                "patch_service_selector"
            )

    if actions & EXECUTABLE_REMEDIATION_ACTIONS:
        try:
            parsed = Diagnosis.model_validate(diagnosis)
            validate_diagnosis_assessment(parsed, state)
        except (ValueError, ValidationError):
            actions -= EXECUTABLE_REMEDIATION_ACTIONS
        else:
            facts = diagnostic_facts(state)
            # A failed interface assertion or current runtime fault is contradictory
            # evidence: investigate it before changing readiness or traffic routing.
            if (not facts['configuration_evidence_complete']
                    or facts['business_status'] == 'failed' or facts['current_runtime_faults']):
                actions -= EXECUTABLE_REMEDIATION_ACTIONS
            if not any(h.status == 'supported'
                       and set(facts['configuration_evidence_ids']) <= set(h.evidence_ids)
                       for h in parsed.assessment.root_cause_hypotheses):
                actions -= EXECUTABLE_REMEDIATION_ACTIONS
    return actions

def _validate_references(
    *,
    plan: RemediationPlan,
    state: IncidentState,
) -> None:
    if len(plan.evidence_ids) != len(
        set(plan.evidence_ids)
    ):
        raise InvalidRemediationPlan(
            "duplicate evidence IDs are not allowed"
        )

    if len(plan.runbook_ids) != len(
        set(plan.runbook_ids)
    ):
        raise InvalidRemediationPlan(
            "duplicate runbook IDs are not allowed"
        )

    available_evidence_ids = {
        item.get("evidence_id")
        for item in state.get("evidence", [])
        if item.get("evidence_id")
    }

    available_runbook_ids = {
        item.get("runbook_id")
        for item in state.get(
            "retrieved_runbooks",
            [],
        )
        if item.get("runbook_id")
    }

    invalid_evidence_ids = (
        set(plan.evidence_ids)
        - available_evidence_ids
    )

    invalid_runbook_ids = (
        set(plan.runbook_ids)
        - available_runbook_ids
    )

    if invalid_evidence_ids:
        raise InvalidRemediationPlan(
            "unknown remediation evidence IDs: "
            f"{sorted(invalid_evidence_ids)!r}"
        )

    if invalid_runbook_ids:
        raise InvalidRemediationPlan(
            "unknown remediation runbook IDs: "
            f"{sorted(invalid_runbook_ids)!r}"
        )

    diagnosis = state.get("diagnosis") or {}

    diagnosis_evidence_ids = set(
        diagnosis.get("evidence_ids", [])
    )
    diagnosis_runbook_ids = set(
        diagnosis.get("runbook_ids", [])
    )

    undeclared_evidence_ids = (
        set(plan.evidence_ids)
        - diagnosis_evidence_ids
    )

    undeclared_runbook_ids = (
        set(plan.runbook_ids)
        - diagnosis_runbook_ids
    )

    if undeclared_evidence_ids:
        raise InvalidRemediationPlan(
            "remediation evidence IDs were not "
            "declared by diagnosis: "
            f"{sorted(undeclared_evidence_ids)!r}"
        )

    if undeclared_runbook_ids:
        raise InvalidRemediationPlan(
            "remediation runbook IDs were not "
            "declared by diagnosis: "
            f"{sorted(undeclared_runbook_ids)!r}"
        )


def _validate_target(
    *,
    plan: RemediationPlan,
    state: IncidentState,
) -> None:
    parameters = plan.parameters
    request = state.get("request", {})

    if parameters.namespace != SAFE_NAMESPACE:
        raise InvalidRemediationPlan(
            "remediation namespace is not allowed"
        )

    if parameters.namespace != request.get(
        "namespace"
    ):
        raise InvalidRemediationPlan(
            "remediation namespace does not "
            "match incident request"
        )

    evidence_type = RESOURCE_EVIDENCE_TYPES[
        parameters.resource_kind
    ]

    matching_evidence = _find_evidence(
        state,
        resource_type=evidence_type,
        resource_name=parameters.resource_name,
    )

    if not matching_evidence:
        raise InvalidRemediationPlan(
            "remediation target does not exist "
            "in collected evidence"
        )


def _validate_text_has_no_commands(
    plan: RemediationPlan,
) -> None:
    named_texts = [
        (
            "summary",
            plan.summary,
        ),
        (
            "expected_result",
            plan.expected_result,
        ),
        (
            "rollback_plan",
            plan.rollback_plan,
        ),
    ]

    named_texts.extend(
        (
            f"investigation_steps[{index}]",
            text,
        )
        for index, text in enumerate(
            plan.parameters.investigation_steps
        )
    )

    for field_name, text in named_texts:
        match = FORBIDDEN_COMMAND_PATTERN.search(
            text
        )

        if match is None:
            continue

        forbidden_token = match.group(0)

        raise InvalidRemediationPlan(
            "remediation plan contains a "
            "forbidden shell command token "
            f"{forbidden_token!r} in "
            f"{field_name}"
        )

def _validate_action_metadata(
    plan: RemediationPlan,
) -> None:
    if plan.action in EXECUTABLE_REMEDIATION_ACTIONS:
        if plan.requires_approval is not True:
            raise InvalidRemediationPlan(
                "executable remediation requires approval"
            )

        if plan.risk_level != "medium":
            raise InvalidRemediationPlan(
                "executable remediation must have "
                "medium risk"
            )

        return

    if plan.requires_approval is not False:
        raise InvalidRemediationPlan(
            "manual investigation must not "
            "request execution approval"
        )

    if plan.risk_level != "low":
        raise InvalidRemediationPlan(
            "manual investigation must have low risk"
        )


def _validate_manual_investigation(
    plan: RemediationPlan,
) -> None:
    parameters = plan.parameters

    if not parameters.investigation_steps:
        raise InvalidRemediationPlan(
            "manual investigation requires steps"
        )

    if (
        parameters.proposed_probe_path is not None
        or parameters.proposed_probe_port is not None
        or parameters.proposed_selector
    ):
        raise InvalidRemediationPlan(
            "manual investigation must not contain "
            "executable mutation parameters"
        )


def _validate_readiness_patch(
    *,
    plan: RemediationPlan,
    state: IncidentState,
) -> None:
    parameters = plan.parameters

    if parameters.resource_kind != "Deployment":
        raise InvalidRemediationPlan(
            "readiness patch target must be a Deployment"
        )

    if not parameters.container_name:
        raise InvalidRemediationPlan(
            "readiness patch requires container_name"
        )

    if not parameters.current_probe_path:
        raise InvalidRemediationPlan(
            "readiness patch requires current probe path"
        )

    if not parameters.proposed_probe_path:
        raise InvalidRemediationPlan(
            "readiness patch requires proposed probe path"
        )

    if not parameters.proposed_probe_path.startswith(
        "/"
    ):
        raise InvalidRemediationPlan(
            "proposed probe path must start with '/'"
        )

    if any(
        character.isspace()
        for character in parameters.proposed_probe_path
    ):
        raise InvalidRemediationPlan(
            "proposed probe path must not contain whitespace"
        )

    deployment_evidence = _find_evidence(
        state,
        resource_type="Deployment",
        resource_name=parameters.resource_name,
    )

    deployment_data = deployment_evidence[0].get(
        "data",
        {},
    )

    containers = deployment_data.get(
        "containers",
        [],
    )

    container = next(
        (
            item
            for item in containers
            if (
                item.get("name")
                == parameters.container_name
            )
        ),
        None,
    )

    if container is None:
        raise InvalidRemediationPlan(
            "container does not exist in "
            "Deployment evidence"
        )

    readiness_probe = (
        container.get("readiness_probe")
        or {}
    )

    if readiness_probe.get("path") != (
        parameters.current_probe_path
    ):
        raise InvalidRemediationPlan(
            "current probe path does not match evidence"
        )

    if readiness_probe.get("port") != (
        parameters.current_probe_port
    ):
        raise InvalidRemediationPlan(
            "current probe port does not match evidence"
        )

    if (
        parameters.current_probe_path
        == parameters.proposed_probe_path
        and parameters.current_probe_port
        == parameters.proposed_probe_port
    ):
        raise InvalidRemediationPlan(
            "proposed probe configuration "
            "does not change current configuration"
        )



def _validate_selector_patch(
    *,
    plan: RemediationPlan,
    state: IncidentState,
) -> None:
    parameters = plan.parameters
    request = state.get("request", {})

    if parameters.resource_kind != "Service":
        raise InvalidRemediationPlan(
            "selector patch target must be a Service"
        )

    if parameters.resource_name != request.get(
        "service_name"
    ):
        raise InvalidRemediationPlan(
            "selector patch must target requested service"
        )

    current_selector = _label_pairs_to_dict(
        parameters.current_selector
    )
    proposed_selector = _label_pairs_to_dict(
        parameters.proposed_selector
    )

    if not proposed_selector:
        raise InvalidRemediationPlan(
            "proposed selector must not be empty"
        )

    service_evidence = _find_evidence(
        state,
        resource_type="Service",
        resource_name=parameters.resource_name,
    )

    actual_selector = service_evidence[0].get(
        "data",
        {},
    ).get("selector", {})

    if current_selector != actual_selector:
        raise InvalidRemediationPlan(
            "current selector does not match evidence"
        )



def validate_remediation_plan(
    *,
    plan: RemediationPlan,
    state: IncidentState,
) -> RemediationPlan:
    diagnosis = state.get("diagnosis") or {}
    fault_category = diagnosis.get(
        "fault_category"
    )

    allowed_actions = (
        get_allowed_remediation_actions(state)
    )

    if plan.action not in ALLOWED_REMEDIATION_ACTIONS:
        raise InvalidRemediationPlan(
            "remediation action is not allowlisted"
        )

    if plan.action not in allowed_actions:
        raise InvalidRemediationPlan(
            "remediation action is not allowed "
            "or not grounded for fault category "
            f"{fault_category!r}"
        )

    _validate_references(
        plan=plan,
        state=state,
    )
    _validate_target(
        plan=plan,
        state=state,
    )
    _validate_text_has_no_commands(plan)
    _validate_action_metadata(plan)
    if plan.action in EXECUTABLE_REMEDIATION_ACTIONS:
        facts = diagnostic_facts(state)
        if not set(facts['configuration_evidence_ids']) <= set(plan.evidence_ids):
            raise InvalidRemediationPlan("write plan must cite Service and registered Deployment configuration")
        if not plan.runbook_ids:
            raise InvalidRemediationPlan("write plan requires a retrieved runbook reference")
        try:
            validate_profile_action(matched_profile(state), plan)
        except ProfileUnavailable as exc:
            raise InvalidRemediationPlan(str(exc)) from exc

    if plan.action == "manual_investigation":
        _validate_manual_investigation(plan)

    elif plan.action == "patch_readiness_probe":
        _validate_readiness_patch(
            plan=plan,
            state=state,
        )

    elif plan.action == "patch_service_selector":
        _validate_selector_patch(
            plan=plan,
            state=state,
        )

    return plan