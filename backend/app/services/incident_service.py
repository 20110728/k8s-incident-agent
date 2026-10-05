# 应用服务层：协调事件创建、查询、图状态读取与人工审批后的工作流恢复。
# 保留仓库层和图检查点的既有一致性机制；API 不直接绕过图调用修改工具。

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4
from backend.app.persistence.operations import approval_binding

from backend.app.persistence.runs import (
    PostgresRunRepository, QueuedModeRequired, QueuedExecutionUnavailable,
    RunNotFound, run_summary, validate_key,
)

from langgraph.types import Command
from pydantic import BaseModel, ValidationError

from backend.app.agent.schemas import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    IncidentRequest,
)
from backend.app.persistence.incidents import (
    IncidentAlreadyExistsError,
    InMemoryIncidentRepository,
    IncidentRepositoryError,
    IncidentRepositoryPort,
    NewIncidentRecord,
)


class GraphStateSnapshotPort(Protocol):
    values: Mapping[str, Any]


class IncidentGraphPort(Protocol):
    def invoke(
        self,
        input: Mapping[str, Any] | Command,
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def get_state(
        self,
        config: Mapping[str, Any],
    ) -> GraphStateSnapshotPort:
        ...


class IncidentServiceError(RuntimeError):
    """Base class for application-service failures."""


class IncidentNotFoundError(IncidentServiceError):
    """Raised when an incident is not present in persistence."""


class IncidentGraphError(IncidentServiceError):
    """Raised when the graph fails or returns invalid state."""


class IncidentApprovalError(IncidentServiceError):
    """Base class for approval submission failures."""


class IncidentNotAwaitingApprovalError(IncidentApprovalError):
    """Raised when an incident has no pending approval interrupt."""


class IncidentApprovalConflictError(IncidentApprovalError):
    """Raised when a decision conflicts with persisted approval state."""


@dataclass(frozen=True)
class IncidentSnapshot:
    incident_id: str
    thread_id: str
    state: dict[str, Any]
    run: dict[str, Any] | None = None
    execution_mode: str = "sync"
    worker_available: bool = False

    @property
    def phase(self) -> str:
        value = self.state.get("phase")
        return value if isinstance(value, str) else "unknown"

    @property
    def waiting_for_approval(self) -> bool:
        return (
            (self.run is None or self.run["status"] == "waiting_approval")
            and self.phase == "awaiting_approval"
            and self.state.get("approval_status")
            == "pending"
        )


def _new_id() -> str:
    return str(uuid4())


def _graph_config(
    thread_id: str,
) -> dict[str, dict[str, str]]:
    return {
        "configurable": {
            "thread_id": thread_id,
        }
    }


def _json_compatible(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")

    if isinstance(value, Mapping):
        return {
            str(key): _json_compatible(child)
            for key, child in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [
            _json_compatible(child)
            for child in value
        ]

    return value


def _normalize_state(
    raw_state: Mapping[str, Any],
    *,
    incident_id: str,
) -> dict[str, Any]:
    state = {
        str(key): _json_compatible(value)
        for key, value in raw_state.items()
        if key != "__interrupt__"
    }

    returned_incident_id = state.get(
        "incident_id"
    )

    if returned_incident_id != incident_id:
        raise IncidentGraphError(
            "graph state incident_id does not "
            "match the requested incident"
        )

    return state


class IncidentApplicationService:
    """Starts, reads, and resumes persistently identified incidents."""

    def __init__(
        self,
        graph: IncidentGraphPort,
        repository: IncidentRepositoryPort | None = None,
        *,
        id_factory: Callable[[], str] = _new_id,
        runs: PostgresRunRepository | None = None,
        execution_mode: str = "sync",
    ) -> None:
        self._graph = graph
        self._repository = (
            repository
            if repository is not None
            else InMemoryIncidentRepository()
        )
        self._id_factory = id_factory
        self._runs = runs
        self._execution_mode = execution_mode
        if execution_mode not in {"sync", "queued"} or (execution_mode == "queued" and runs is None):
            raise ValueError("queued mode requires a durable run repository")

    def _delete_failed_incident(
        self,
        incident_id: str,
    ) -> None:
        try:
            self._repository.delete(incident_id)
        except IncidentRepositoryError:
            return

    def create_incident(
        self,
        request: IncidentRequest,
        *,
        idempotency_key: str | None = None,
    ) -> IncidentSnapshot:
        validated_request = (
            IncidentRequest.model_validate(request)
        )
        validate_key(idempotency_key)
        if self._execution_mode == "queued":
            row = self._runs.accept(
                incident_id=self._id_factory(), run_id=_new_id(), thread_id=_new_id(),
                payload=validated_request.model_dump(mode="json"), key=idempotency_key,
            )
            return self._snapshot_for_run(row)
        if idempotency_key is not None:
            raise QueuedModeRequired()
        incident_id = self._id_factory().strip()

        if not incident_id:
            raise IncidentServiceError(
                "id_factory returned a blank incident ID"
            )

        thread_id = incident_id

        try:
            self._repository.create(
                NewIncidentRecord(
                    incident_id=incident_id,
                    thread_id=thread_id,
                    namespace=(
                        validated_request.namespace
                    ),
                    service_name=(
                        validated_request.service_name
                    ),
                    description=(
                        validated_request.description
                    ),
                )
            )
        except IncidentAlreadyExistsError as error:
            raise IncidentServiceError(
                "id_factory returned a duplicate incident ID"
            ) from error
        except IncidentRepositoryError as error:
            raise IncidentServiceError(
                "failed to persist incident metadata"
            ) from error

        try:
            result = self._graph.invoke(
                {
                    "incident_id": incident_id,
                    "request": (
                        validated_request.model_dump()
                    ),
                },
                config=_graph_config(thread_id),
            )

            if not isinstance(result, Mapping):
                raise IncidentGraphError(
                    "graph invoke did not return a mapping"
                )

            state = _normalize_state(
                result,
                incident_id=incident_id,
            )

        except IncidentGraphError:
            self._delete_failed_incident(incident_id)
            raise

        except Exception as error:
            self._delete_failed_incident(incident_id)
            raise IncidentGraphError(
                "incident graph invocation failed"
            ) from error

        try:
            updated = self._repository.update_phase(
                incident_id,
                str(state.get("phase") or "unknown"),
            )
        except IncidentRepositoryError as error:
            raise IncidentServiceError(
                "failed to update incident metadata"
            ) from error

        if updated is None:
            raise IncidentServiceError(
                "incident metadata disappeared after graph invocation"
            )

        return IncidentSnapshot(
            incident_id=incident_id,
            thread_id=thread_id,
            state=state,
        )

    def get_incident(
        self,
        incident_id: str,
    ) -> IncidentSnapshot:
        normalized_id = incident_id.strip()

        try:
            record = self._repository.get(
                normalized_id
            )
        except IncidentRepositoryError as error:
            raise IncidentServiceError(
                "incident metadata lookup failed"
            ) from error

        if record is None:
            raise IncidentNotFoundError(
                f"incident {normalized_id!r} was not found"
            )

        run = self._runs.latest(normalized_id) if self._runs is not None else None
        if run is not None:
            return self._snapshot_for_run(run)

        try:
            snapshot = self._graph.get_state(
                _graph_config(record.thread_id)
            )
            values = snapshot.values

            if not isinstance(values, Mapping):
                raise IncidentGraphError(
                    "graph state snapshot did not "
                    "contain mapping values"
                )

            state = _normalize_state(
                values,
                incident_id=normalized_id,
            )

        except IncidentGraphError:
            raise

        except Exception as error:
            raise IncidentGraphError(
                "incident graph state lookup failed"
            ) from error

        return IncidentSnapshot(
            incident_id=normalized_id,
            thread_id=record.thread_id,
            state=state,
            execution_mode=self._execution_mode,
        )

    def _snapshot_for_run(self, row: dict) -> IncidentSnapshot:
        if row["status"] == "queued" and row["attempt"] == 0:
            state = {"incident_id": row["incident_id"], "request": row["input_payload"], "phase": "created"}
        else:
            try:
                values = self._graph.get_state(_graph_config(row["thread_id"])).values
                if not values and not row.get("checkpoint_started", False) and (row["status"] in {"running", "retry_scheduled"} or
                        (row.get("last_error") or {}).get("code") in {
                            "WORKER_FAILED", "ATTEMPTS_EXHAUSTED", "UNSUPPORTED_OR_CORRUPT_INPUT"}):
                    values = {"incident_id": row["incident_id"], "request": row["input_payload"],
                              "phase": "failed" if row["status"] == "failed" else "created"}
                if not isinstance(values, Mapping) or not values:
                    raise IncidentGraphError("executed run checkpoint is missing")
                state = _normalize_state(values, incident_id=row["incident_id"])
            except IncidentGraphError:
                raise
            except Exception as error:
                raise IncidentGraphError("run checkpoint lookup failed") from error
        return IncidentSnapshot(row["incident_id"], row["thread_id"], state,
                                run=run_summary(row), execution_mode=self._execution_mode,
                                worker_available=self._runs.worker_available())

    def get_by_idempotency_key(self, key: str) -> IncidentSnapshot:
        row = self._runs.by_key(key) if self._runs is not None else None
        if row is None:
            raise RunNotFound()
        return self._snapshot_for_run(row)

    def list_metadata(self, **kwargs) -> dict:
        if self._runs is None:
            raise RunNotFound()
        return self._runs.list_metadata(**kwargs)

    def list_operations(self, incident_id: str) -> dict:
        if self._runs is None or self._repository.get(incident_id) is None:
            raise RunNotFound()
        return {"items": self._runs.history(incident_id)}

    def submit_approval(
        self,
        incident_id: str,
        decision: ApprovalDecision,
    ) -> IncidentSnapshot:
        """Resume one pending graph or return its idempotent result."""

        try:
            validated_decision = (
                ApprovalDecision.model_validate(decision)
            )
        except ValidationError as error:
            raise IncidentApprovalConflictError(
                "approval decision is invalid"
            ) from error

        current = self.get_incident(incident_id)
        if self._execution_mode == "queued" and current.run is None:
            raise QueuedExecutionUnavailable()
        state = current.state
        if current.run is not None:
            row = self._runs.latest(current.incident_id)
            saved = row.get("approval_payload") if row else None
            if saved:
                if saved["decision"] != validated_decision.model_dump(mode="json"):
                    raise IncidentApprovalConflictError("approval has already been decided differently")
                return current
        raw_record = state.get("approval_record")

        if raw_record is not None:
            try:
                approval_record = (
                    ApprovalRecord.model_validate(raw_record)
                )
            except ValidationError as error:
                raise IncidentGraphError(
                    "graph state contains an invalid approval record"
                ) from error

            if approval_record.incident_id != current.incident_id:
                raise IncidentGraphError(
                    "approval record incident_id does not match "
                    "the requested incident"
                )

            if (
                approval_record.approval_id
                != validated_decision.approval_id
            ):
                raise IncidentApprovalConflictError(
                    "approval ID conflicts with the recorded decision"
                )

            if (
                approval_record.approved
                == validated_decision.approved
                and approval_record.approver
                == validated_decision.approver
                and approval_record.comment
                == validated_decision.comment
            ):
                return current

            raise IncidentApprovalConflictError(
                "approval has already been decided differently"
            )

        if not current.waiting_for_approval:
            raise IncidentNotAwaitingApprovalError(
                "incident is not awaiting approval"
            )

        try:
            approval_request = ApprovalRequest.model_validate(
                state.get("approval_request")
            )
        except ValidationError as error:
            raise IncidentGraphError(
                "graph state contains an invalid approval request"
            ) from error

        if approval_request.incident_id != current.incident_id:
            raise IncidentGraphError(
                "approval request incident_id does not match "
                "the requested incident"
            )

        if (
            approval_request.approval_id
            != validated_decision.approval_id
        ):
            raise IncidentApprovalConflictError(
                "approval decision does not match the pending request"
            )

        if current.run is not None:
            self._runs.queue_approval(current.run["run_id"],
                                      validated_decision.model_dump(mode="json"), approval_binding(state))
            return self.get_incident(incident_id)

        try:
            result = self._graph.invoke(
                Command(
                    resume=validated_decision.model_dump(
                        mode="json"
                    )
                ),
                config=_graph_config(current.thread_id),
            )

            if not isinstance(result, Mapping):
                raise IncidentGraphError(
                    "graph approval resume did not return a mapping"
                )

            resumed_state = _normalize_state(
                result,
                incident_id=current.incident_id,
            )

            try:
                resumed_record = ApprovalRecord.model_validate(
                    resumed_state.get("approval_record")
                )
            except ValidationError as error:
                raise IncidentGraphError(
                    "graph approval resume did not persist a valid "
                    "approval record"
                ) from error

            if (
                resumed_record.incident_id
                != current.incident_id
                or resumed_record.approval_id
                != validated_decision.approval_id
                or resumed_record.approved
                != validated_decision.approved
                or resumed_record.approver
                != validated_decision.approver
                or resumed_record.comment
                != validated_decision.comment
            ):
                raise IncidentGraphError(
                    "graph approval record does not match the "
                    "submitted decision"
                )

        except IncidentGraphError:
            raise
        except Exception as error:
            raise IncidentGraphError(
                "incident graph approval resume failed"
            ) from error

        phase = str(
            resumed_state.get("phase") or "unknown"
        )

        try:
            updated = self._repository.update_phase(
                current.incident_id,
                phase,
            )
        except IncidentRepositoryError as error:
            raise IncidentServiceError(
                "failed to update incident metadata after approval"
            ) from error

        if updated is None:
            raise IncidentServiceError(
                "incident metadata disappeared after approval"
            )

        return IncidentSnapshot(
            incident_id=current.incident_id,
            thread_id=current.thread_id,
            state=resumed_state,
        )
