from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    Path,
    status,
)

from backend.app.api.dependencies import (
    get_incident_service,
)
from backend.app.api.errors import ApiError
from backend.app.api.schemas import (
    CreateIncidentRequest,
    ErrorResponse,
    IncidentStatusResponse,
)
from backend.app.services.incident_service import (
    IncidentApplicationService,
    IncidentGraphError,
    IncidentNotFoundError,
    IncidentServiceError,
    IncidentSnapshot,
)


router = APIRouter(
    prefix="/incidents",
    tags=["incidents"],
)

IncidentServiceDependency = Annotated[
    IncidentApplicationService,
    Depends(get_incident_service),
]


def _response_from_snapshot(
    snapshot: IncidentSnapshot,
) -> IncidentStatusResponse:
    return IncidentStatusResponse.from_state(
        incident_id=snapshot.incident_id,
        thread_id=snapshot.thread_id,
        state=snapshot.state,
        waiting_for_approval=(
            snapshot.waiting_for_approval
        ),
    )


def _raise_graph_error(
    error: IncidentGraphError,
) -> None:
    raise ApiError(
        status_code=status.HTTP_502_BAD_GATEWAY,
        code="INCIDENT_PROCESSING_FAILED",
        message=(
            "The incident workflow could not be "
            "processed."
        ),
    ) from error


@router.post(
    "",
    response_model=IncidentStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "The incident request is invalid.",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "The incident service failed.",
        },
        status.HTTP_502_BAD_GATEWAY: {
            "model": ErrorResponse,
            "description": "The workflow dependency failed.",
        },
    },
    summary="Create and start an incident",
    description=(
        "Creates an incident and runs its LangGraph workflow "
        "until a terminal state or human-approval interrupt."
    ),
)
def create_incident(
    request: CreateIncidentRequest,
    service: IncidentServiceDependency,
) -> IncidentStatusResponse:
    try:
        snapshot = service.create_incident(request)

    except IncidentGraphError as error:
        _raise_graph_error(error)

    except IncidentServiceError as error:
        raise ApiError(
            status_code=(
                status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            code="INCIDENT_SERVICE_ERROR",
            message="The incident service failed.",
        ) from error

    return _response_from_snapshot(snapshot)


@router.get(
    "/{incident_id}",
    response_model=IncidentStatusResponse,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": "The incident does not exist.",
        },
        status.HTTP_502_BAD_GATEWAY: {
            "model": ErrorResponse,
            "description": "The checkpoint lookup failed.",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "The incident service failed.",
        },
    },
    summary="Get current incident state",
    description=(
        "Returns the latest checkpointed state for one "
        "incident without rerunning its workflow."
    ),
)
def get_incident(
    incident_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=128,
            pattern=r"^[a-zA-Z0-9-]+$",
        ),
    ],
    service: IncidentServiceDependency,
) -> IncidentStatusResponse:
    try:
        snapshot = service.get_incident(
            incident_id
        )

    except IncidentNotFoundError as error:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="INCIDENT_NOT_FOUND",
            message="The requested incident was not found.",
        ) from error

    except IncidentGraphError as error:
        _raise_graph_error(error)

    except IncidentServiceError as error:
        raise ApiError(
            status_code=(
                status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            code="INCIDENT_SERVICE_ERROR",
            message="The incident service failed.",
        ) from error

    return _response_from_snapshot(snapshot)