"""Accept once, find by client key and poll. No model or collector on HTTP threads."""
from datetime import UTC, datetime
from functools import partial

from fastapi import APIRouter, Depends, Request, Response, Query
from fastapi.encoders import jsonable_encoder

from backend.app.api.dependencies import get_incident_service
from backend.app.api.errors import ApiError
from backend.app.api.routes.messages import IncidentId
from backend.app.api.routes.rounds import snapshot_read
from backend.app.persistence.database import connect_database
from backend.app.persistence.settings import get_database_settings
from backend.app.persistence.interactions import InteractionRepository
from backend.app.persistence.rounds import RoundNotFound
from backend.app.persistence.runs import request_digest
from backend.app.services.interaction_schemas import CreateInteraction, interaction_view

router = APIRouter(prefix="/incidents", tags=["interactions"])


def get_interaction_repository():
    return InteractionRepository(partial(connect_database, get_database_settings()))


@router.get("/{incident_id}/interaction-status")
def status(incident_id: IncidentId, repo=Depends(get_interaction_repository)):
    return repo.status(incident_id)


@router.get("/{incident_id}/interactions")
def find(incident_id: IncidentId, client_message_id: str = Query(min_length=1, max_length=128), repo=Depends(get_interaction_repository)):
    row = repo.by_interaction_key(incident_id, client_message_id)
    if row is None:
        from backend.app.persistence.controls import ControlRepository
        return ControlRepository(repo._connect).find_control(incident_id, client_message_id)
    return interaction_view(row)


@router.get("/{incident_id}/interactions/{run_id}")
def read(incident_id: IncidentId, run_id: IncidentId, repo=Depends(get_interaction_repository)):
    row = repo.get_round(incident_id, run_id)
    if row["run_kind"] != "interaction":
        raise RoundNotFound()
    return interaction_view(row)


@router.post("/{incident_id}/interactions", status_code=202)
def create(incident_id: IncidentId, body: CreateInteraction, request: Request, response: Response,
           repo=Depends(get_interaction_repository)):
    if body.intent == "status":
        response.status_code = 200
        return repo.status(incident_id)
    if request.app.state.settings.execution_mode != "queued":
        raise ApiError(status_code=409, code="INTERACTIONS_REQUIRE_QUEUED", message="Interactions require queued mode.")
    replay = repo.by_interaction_key(incident_id, body.client_message_id)
    if replay:
        return interaction_view(repo._replay(replay, request_digest(body.model_dump())))
    prior = repo.latest(incident_id)
    explicit_stop = body.intent == "stop" or (body.intent == "auto" and body.content.strip() in {"先别查了", "停止调查", "停止", "stop"})
    saved_control = repo._read("SELECT control_id FROM incident_agent_app.controls WHERE incident_id=%s AND client_message_id=%s", (incident_id, body.client_message_id))
    if saved_control and not explicit_stop and body.intent not in {"supplement", "investigate"}:
        from backend.app.persistence.runs import IdempotencyConflict
        raise IdempotencyConflict()
    if saved_control or explicit_stop or (body.intent in {"supplement", "investigate"} and prior and
                         (prior["status"] not in {"succeeded", "failed", "cancelled"} or prior.get("invalidated_at"))):
        from backend.app.api.routes.controls import control
        from backend.app.persistence.controls import ControlRepository
        from backend.app.services.control_schemas import ControlRequest
        command = ControlRequest(client_message_id=body.client_message_id, content=body.content,
                                 action="stop" if explicit_stop else body.intent)
        return control(incident_id, command, request, ControlRepository(repo._connect))
    service = get_incident_service(request)

    def reference(run_id):
        row = repo.get_round(incident_id, run_id) if run_id and run_id != "legacy" else None
        if row and row["run_kind"] != "diagnosis":
            raise RoundNotFound()
        snapshot = snapshot_read(lambda: service.get_run_snapshot(row) if row else service.get_legacy_snapshot(incident_id))
        return {"run_id": row["run_id"] if row else None, "snapshot_at": datetime.now(UTC).isoformat(),
                "state": jsonable_encoder(snapshot.state)}

    selected = body.reference_run_id or (prior["run_id"] if prior else None)
    references = [reference(selected)]
    if body.compare_run_id:
        if body.compare_run_id == (selected or "legacy"):
            raise ApiError(status_code=422, code="INTERACTION_REFERENCE_INVALID", message="Compare requires two different rounds.")
        references.append(reference(body.compare_run_id))
    return interaction_view(repo.accept_interaction(incident_id, body, prior["run_id"] if prior else None, references))
