"""Explicit input controls and version-bound replies; all writes are queued-only."""
from functools import partial

from fastapi import APIRouter, Depends, Request, Query
from fastapi.encoders import jsonable_encoder

from backend.app.api.dependencies import get_incident_service
from backend.app.api.errors import ApiError
from backend.app.api.routes.messages import IncidentId
from backend.app.api.routes.rounds import snapshot_read
from backend.app.persistence.database import connect_database
from backend.app.persistence.settings import get_database_settings
from backend.app.persistence.controls import ControlRepository
from backend.app.services.control_schemas import ControlRequest, AnswerQuestion

router = APIRouter(prefix="/incidents", tags=["controls"])


def get_control_repository():
    return ControlRepository(partial(connect_database, get_database_settings()))


def queued(request):
    if request.app.state.settings.execution_mode != "queued":
        raise ApiError(status_code=409, code="CONTROLS_REQUIRE_QUEUED", message="Controls require queued mode.")


@router.get("/{incident_id}/controls")
def find(incident_id: IncidentId, client_message_id: str = Query(min_length=1, max_length=128), repo=Depends(get_control_repository)):
    return repo.find_control(incident_id, client_message_id)


@router.post("/{incident_id}/controls", status_code=202)
def control(incident_id: IncidentId, body: ControlRequest, request: Request, repo=Depends(get_control_repository)):
    queued(request)
    # Replays can succeed without checkpoint/model availability.
    existing = repo._read("SELECT parent_run_id,snapshot FROM incident_agent_app.controls WHERE incident_id=%s AND client_message_id=%s", (incident_id, body.client_message_id))
    if existing:
        return repo.control(incident_id, body, existing[0]["parent_run_id"], existing[0]["snapshot"])
    row = repo.latest(incident_id)
    if row is None:
        from backend.app.persistence.rounds import RoundConflict
        raise RoundConflict()
    service = get_incident_service(request)
    snapshot = snapshot_read(lambda: service.get_run_snapshot(row))
    return repo.control(incident_id, body, row["run_id"], jsonable_encoder(snapshot.state))


@router.post("/{incident_id}/runs/{run_id}/answers", status_code=202)
def answer(incident_id: IncidentId, run_id: IncidentId, body: AnswerQuestion, request: Request, repo=Depends(get_control_repository)):
    queued(request)
    return repo.answer(incident_id, run_id, body)
