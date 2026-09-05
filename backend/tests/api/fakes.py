from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from backend.app.agent.schemas import (
    ApprovalDecision,
    IncidentRequest,
)

from backend.app.services.incident_service import (
    IncidentSnapshot,
)

from dataclasses import replace
from datetime import UTC, datetime

from backend.app.persistence.incidents import (
    IncidentAlreadyExistsError,
    IncidentRecord,
    NewIncidentRecord,
)

class FakeIncidentGraph:
    def __init__(
        self,
        *,
        result: Mapping[str, Any] | None = None,
        resume_result: Mapping[str, Any] | None = None,
        invoke_error: Exception | None = None,
        get_state_error: Exception | None = None,
    ) -> None:
        self.result = dict(result or {})
        self.resume_result = dict(resume_result or {})
        self.invoke_error = invoke_error
        self.get_state_error = get_state_error
        self.invocations: list[dict[str, Any]] = []
        self.resume_calls: list[dict[str, Any]] = []
        self.state_reads: list[dict[str, Any]] = []
        self.states: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _thread_id(
        config: Mapping[str, Any],
    ) -> str:
        return str(
            config["configurable"]["thread_id"]
        )

    def invoke(
        self,
        input: Any,
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        thread_id = self._thread_id(config)

        if not isinstance(input, Mapping):
            resume_value = getattr(input, "resume", None)
            self.resume_calls.append(
                {
                    "resume": resume_value,
                    "config": dict(config),
                }
            )

            if self.invoke_error is not None:
                raise self.invoke_error

            result = {
                **self.states.get(thread_id, {}),
                **self.resume_result,
            }
            self.states[thread_id] = dict(result)
            return result

        self.invocations.append(
            {
                "input": dict(input),
                "config": dict(config),
            }
        )

        if self.invoke_error is not None:
            raise self.invoke_error

        result = {
            **dict(input),
            **self.result,
        }
        self.states[thread_id] = {
            key: value
            for key, value in result.items()
            if key != "__interrupt__"
        }

        return result

    def get_state(
        self,
        config: Mapping[str, Any],
    ) -> SimpleNamespace:
        self.state_reads.append(dict(config))

        if self.get_state_error is not None:
            raise self.get_state_error

        thread_id = self._thread_id(config)

        return SimpleNamespace(
            values=self.states[thread_id]
        )

class FakeIncidentRepository:
    def __init__(self) -> None:
        self.records: dict[str, IncidentRecord] = {}
        self.create_calls: list[NewIncidentRecord] = []
        self.get_calls: list[str] = []
        self.update_phase_calls: list[
            tuple[str, str]
        ] = []
        self.delete_calls: list[str] = []
        self.create_error: Exception | None = None
        self.get_error: Exception | None = None
        self.update_phase_error: Exception | None = None
        self.delete_error: Exception | None = None

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def create(
        self,
        record: NewIncidentRecord,
    ) -> IncidentRecord:
        self.create_calls.append(record)

        if self.create_error is not None:
            raise self.create_error

        if record.incident_id in self.records:
            raise IncidentAlreadyExistsError(
                "fake duplicate incident"
            )

        now = self._now()
        stored = IncidentRecord(
            incident_id=record.incident_id,
            thread_id=record.thread_id,
            namespace=record.namespace,
            service_name=record.service_name,
            description=record.description,
            phase=record.phase,
            created_at=now,
            updated_at=now,
        )
        self.records[record.incident_id] = stored
        return stored

    def get(
        self,
        incident_id: str,
    ) -> IncidentRecord | None:
        self.get_calls.append(incident_id)

        if self.get_error is not None:
            raise self.get_error

        return self.records.get(incident_id)

    def update_phase(
        self,
        incident_id: str,
        phase: str,
    ) -> IncidentRecord | None:
        self.update_phase_calls.append(
            (incident_id, phase)
        )

        if self.update_phase_error is not None:
            raise self.update_phase_error

        record = self.records.get(incident_id)

        if record is None:
            return None

        updated = replace(
            record,
            phase=phase,
            updated_at=self._now(),
        )
        self.records[incident_id] = updated
        return updated

    def delete(self, incident_id: str) -> bool:
        self.delete_calls.append(incident_id)

        if self.delete_error is not None:
            raise self.delete_error

        return self.records.pop(
            incident_id,
            None,
        ) is not None

class FakeIncidentService:
    def __init__(
        self,
        *,
        create_result: IncidentSnapshot | None = None,
        get_result: IncidentSnapshot | None = None,
        approval_result: IncidentSnapshot | None = None,
        create_error: Exception | None = None,
        get_error: Exception | None = None,
        approval_error: Exception | None = None,
    ) -> None:
        self.create_result = create_result
        self.get_result = get_result
        self.approval_result = approval_result
        self.create_error = create_error
        self.get_error = get_error
        self.approval_error = approval_error

        self.create_calls: list[IncidentRequest] = []
        self.get_calls: list[str] = []
        self.approval_calls: list[
            tuple[str, ApprovalDecision]
        ] = []

    def create_incident(
        self,
        request: IncidentRequest,
    ) -> IncidentSnapshot:
        self.create_calls.append(request)

        if self.create_error is not None:
            raise self.create_error

        if self.create_result is None:
            raise AssertionError(
                "fake create result was not configured"
            )

        return self.create_result

    def get_incident(
        self,
        incident_id: str,
    ) -> IncidentSnapshot:
        self.get_calls.append(incident_id)

        if self.get_error is not None:
            raise self.get_error

        if self.get_result is None:
            raise AssertionError(
                "fake get result was not configured"
            )

        return self.get_result

    def submit_approval(
        self,
        incident_id: str,
        decision: ApprovalDecision,
    ) -> IncidentSnapshot:
        self.approval_calls.append(
            (incident_id, decision)
        )

        if self.approval_error is not None:
            raise self.approval_error

        if self.approval_result is None:
            raise AssertionError(
                "fake approval result was not configured"
            )

        return self.approval_result