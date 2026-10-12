"""Storage-only message endpoints. Never initialize model/cluster/checkpointer."""
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from backend.app.api.errors import ApiError
from backend.app.persistence.database import connect_database
from backend.app.persistence.messages import PostgresMessageRepository, MessageError, MessageStorageError
from backend.app.persistence.settings import get_database_settings
from backend.app.services.message_schemas import CreateMessage, MessageDraft, MessageReceipt, MessagePage

router = APIRouter(prefix="/incidents", tags=["messages"])
IncidentId = Annotated[str, Path(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9-]+$")]


def get_message_repository():
    return PostgresMessageRepository(partial(connect_database, get_database_settings()))


def perform(operation):
    try:
        return operation()
    except MessageError as error:
        raise ApiError(status_code=error.status, code=error.code, message=str(error)) from error
    except MessageStorageError as error:
        raise ApiError(status_code=503, code="MESSAGE_STORAGE_UNAVAILABLE",
                       message="Could not save/read messages.") from error


@router.post("/{incident_id}/messages", response_model=MessageReceipt, status_code=201,
             summary="Save a message only; does not start or resume a workflow")
def create_message(incident_id: IncidentId, body: CreateMessage, request: Request,
                   response: Response, repository=Depends(get_message_repository)):
    if request.app.state.settings.execution_mode != "queued":
        raise ApiError(status_code=409, code="MESSAGES_REQUIRE_QUEUED", message="Messages require queued mode.")
    result = perform(lambda: repository.append(incident_id, MessageDraft(**body.model_dump())))
    response.status_code = 201 if result.created else 200
    return result


@router.get("/{incident_id}/messages", response_model=MessagePage)
def list_messages(incident_id: IncidentId, limit: Annotated[int, Query(ge=1, le=50)] = 20,
                  before_sequence: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
                  repository=Depends(get_message_repository)):
    return perform(lambda: repository.list(incident_id, limit, before_sequence))
