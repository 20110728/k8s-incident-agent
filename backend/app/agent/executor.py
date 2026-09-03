from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from backend.app.agent.execution_policy import (
    ExecutionAuthorization,
    InvalidExecutionAuthorization,
    validate_execution_authorization,
)
from backend.app.agent.schemas import (
    ActionExecutionResult,
    LabelPair,
    ResourceMutationResult,
)
from backend.app.agent.state import IncidentState
from backend.app.tools.client import (
    KubernetesClients,
)
from backend.app.tools.remediation_tools import (
    patch_readiness_probe,
    patch_service_selector,
)


class RemediationExecutorPort(Protocol):
    def execute(
        self,
        state: IncidentState,
    ) -> ActionExecutionResult:
        ...


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _label_pairs_to_dict(
    pairs: list[LabelPair],
) -> dict[str, str]:
    result: dict[str, str] = {}

    for pair in pairs:
        if pair.key in result:
            raise ValueError(
                "selector contains duplicate keys"
            )

        result[pair.key] = pair.value

    return result


class KubernetesRemediationExecutor:
    def __init__(
        self,
        *,
        clients: KubernetesClients,
        patch_service_selector_fn: Callable[
            ...,
            ResourceMutationResult,
        ] = patch_service_selector,
        patch_readiness_probe_fn: Callable[
            ...,
            ResourceMutationResult,
        ] = patch_readiness_probe,
        now: Callable[[], str] = utc_now,
    ) -> None:
        self._clients = clients
        self._patch_service_selector = (
            patch_service_selector_fn
        )
        self._patch_readiness_probe = (
            patch_readiness_probe_fn
        )
        self._now = now

    def _execute_service_selector(
        self,
        authorization: ExecutionAuthorization,
    ) -> ResourceMutationResult:
        parameters = (
            authorization.plan.parameters
        )

        current_selector = (
            _label_pairs_to_dict(
                parameters.current_selector
            )
        )
        proposed_selector = (
            _label_pairs_to_dict(
                parameters.proposed_selector
            )
        )

        return self._patch_service_selector(
            clients=self._clients,
            namespace=parameters.namespace,
            service_name=(
                parameters.resource_name
            ),
            expected_selector=current_selector,
            proposed_selector=proposed_selector,
        )

    def _execute_readiness_probe(
        self,
        authorization: ExecutionAuthorization,
    ) -> ResourceMutationResult:
        parameters = (
            authorization.plan.parameters
        )

        if parameters.container_name is None:
            raise ValueError(
                "approved readiness action is "
                "missing container_name"
            )

        if parameters.current_probe_path is None:
            raise ValueError(
                "approved readiness action is "
                "missing current_probe_path"
            )

        if parameters.proposed_probe_path is None:
            raise ValueError(
                "approved readiness action is "
                "missing proposed_probe_path"
            )

        if parameters.current_probe_port is None:
            raise ValueError(
                "approved readiness action is "
                "missing current_probe_port"
            )

        if parameters.proposed_probe_port is None:
            raise ValueError(
                "approved readiness action is "
                "missing proposed_probe_port"
            )

        return self._patch_readiness_probe(
            clients=self._clients,
            namespace=parameters.namespace,
            deployment_name=(
                parameters.resource_name
            ),
            container_name=(
                parameters.container_name
            ),
            expected_path=(
                parameters.current_probe_path
            ),
            proposed_path=(
                parameters.proposed_probe_path
            ),
            expected_port=(
                parameters.current_probe_port
            ),
            proposed_port=(
                parameters.proposed_probe_port
            ),
        )

    def _execute_authorized_action(
        self,
        authorization: ExecutionAuthorization,
    ) -> ResourceMutationResult:
        action = authorization.plan.action

        if action == "patch_service_selector":
            return self._execute_service_selector(
                authorization
            )

        if action == "patch_readiness_probe":
            return self._execute_readiness_probe(
                authorization
            )

        raise InvalidExecutionAuthorization(
            f"action {action!r} is not executable"
        )

    def execute(
        self,
        state: IncidentState,
    ) -> ActionExecutionResult:
        authorization = (
            validate_execution_authorization(
                state
            )
        )

        if authorization.already_executed:
            previous_result = (
                authorization.previous_result
            )

            if previous_result is None:
                raise RuntimeError(
                    "authorization marked execution "
                    "as completed without a result"
                )

            return previous_result

        started_at = self._now()

        try:
            mutation_result = (
                self._execute_authorized_action(
                    authorization
                )
            )

            if not isinstance(
                mutation_result,
                ResourceMutationResult,
            ):
                mutation_result = (
                    ResourceMutationResult.model_validate(
                        mutation_result
                    )
                )

        except InvalidExecutionAuthorization:
            raise

        except Exception as error:
            mutation_result = ResourceMutationResult(
                status="failed",
                before_snapshot=None,
                after_snapshot=None,
                applied_patch={},
                rollback_patch={},
                message=(
                    "Remediation tool raised an "
                    "unexpected exception."
                ),
                error_code=(
                    "REMEDIATION_TOOL_ERROR"
                ),
                error_message=str(error),
            )

        finished_at = self._now()
        plan = authorization.plan

        return ActionExecutionResult(
            execution_id=(
                authorization.execution_id
            ),
            approval_id=(
                authorization.approval_id
            ),
            action=plan.action,
            status=mutation_result.status,
            namespace=(
                plan.parameters.namespace
            ),
            resource_kind=(
                plan.parameters.resource_kind
            ),
            resource_name=(
                plan.parameters.resource_name
            ),
            started_at=started_at,
            finished_at=finished_at,
            before_snapshot=(
                mutation_result.before_snapshot
            ),
            after_snapshot=(
                mutation_result.after_snapshot
            ),
            applied_patch=(
                mutation_result.applied_patch
            ),
            rollback_patch=(
                mutation_result.rollback_patch
            ),
            message=mutation_result.message,
            error_code=(
                mutation_result.error_code
            ),
            error_message=(
                mutation_result.error_message
            ),
        )