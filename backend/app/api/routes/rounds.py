"""Explicit next round API; message intent classification is deferred to 4B."""
from functools import partial

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.app.api.dependencies import get_incident_service
from backend.app.api.errors import ApiError
from backend.app.api.routes.messages import IncidentId
from backend.app.api.routes.incidents import _response_from_snapshot
from backend.app.persistence.database import connect_database
from backend.app.persistence.settings import get_database_settings
from backend.app.persistence.rounds import RoundRepository, RoundConflict
from backend.app.persistence.runs import request_digest, run_summary
from backend.app.services.incident_service import IncidentNotFoundError, IncidentGraphError

router = APIRouter(prefix="/incidents", tags=["rounds"])


class CreateRound(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    message_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9-]+$")


def get_round_repository():
    return RoundRepository(partial(connect_database, get_database_settings()))


def snapshot_read(operation):
    try:
        return operation()
    except IncidentNotFoundError as error:
        raise ApiError(status_code=404, code="INCIDENT_NOT_FOUND", message=str(error)) from error
    except IncidentGraphError as error:
        raise ApiError(status_code=503, code="ROUND_HISTORY_UNAVAILABLE", message="Could not read historical state.") from error


@router.post("/{incident_id}/runs", status_code=202)
def create_round(incident_id: IncidentId, body: CreateRound, request: Request,
                 repo=Depends(get_round_repository), service=Depends(get_incident_service)):
    if request.app.state.settings.execution_mode != "queued":
        raise ApiError(status_code=409, code="ROUNDS_REQUIRE_QUEUED", message="Rounds require queued mode.")
    row = repo.by_round_key(incident_id, body.client_request_id)
    if row:
        row = repo._replay(row, request_digest({"message_id": body.message_id}))
    else:
        prior = repo.latest(incident_id)
        if prior and prior["status"] not in {"succeeded", "failed", "cancelled"}:
            raise RoundConflict()
        snapshot = snapshot_read(lambda: service.get_run_snapshot(prior) if prior else service.get_incident(incident_id))
        row = repo.accept_round(incident_id, body.message_id, body.client_request_id,
                                prior["run_id"] if prior else None, snapshot.state)
    return {"run": run_summary(row), "incident_id": incident_id,
            "parent_run_id": row["parent_run_id"], "source_message_id": row["source_message_id"],
            "input_message_sequence": row["input_message_sequence"], "workflow_version": row["workflow_version"]}


@router.get("/{incident_id}/runs/{run_id}")
def read_round(incident_id: IncidentId, run_id: IncidentId,
               repo=Depends(get_round_repository), service=Depends(get_incident_service)):
    # 'legacy' addresses the original event thread without resetting it.
    if run_id == "legacy":
        result = snapshot_read(lambda: service.get_legacy_snapshot(incident_id))
        return {"result": _response_from_snapshot(result), "context": None, "parent_run_id": None}
    row = repo.get_round(incident_id, run_id)
    if row["run_kind"] == "interaction":
        from backend.app.services.interaction_schemas import interaction_view
        return interaction_view(row)
    result = snapshot_read(lambda: service.get_run_snapshot(row))
    return {"result": _response_from_snapshot(result), "context": row["context_snapshot"],
            "parent_run_id": row["parent_run_id"], "source_message_id": row["source_message_id"],
            "input_message_sequence": row["input_message_sequence"], "workflow_version": row["workflow_version"]}
