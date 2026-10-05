from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    Header,
    Query,
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
    RunSummary,
    SubmitApprovalRequest,
)
from backend.app.services.incident_service import (
    IncidentApplicationService,
    IncidentApprovalConflictError,
    IncidentGraphError,
    IncidentNotAwaitingApprovalError,
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
    response = IncidentStatusResponse.from_state(
        incident_id=snapshot.incident_id,
        thread_id=snapshot.thread_id,
        state=snapshot.state,
        waiting_for_approval=(
            snapshot.waiting_for_approval
        ),
    )
    response.run = RunSummary.model_validate(snapshot.run) if snapshot.run is not None else None
    response.execution_mode = snapshot.execution_mode
    response.worker_available = snapshot.worker_available
    return response


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
    summary="Create an incident in the configured execution mode",
    description=(
        "Queued mode commits the incident and run without executing a workflow. "
        "Sync mode runs the legacy workflow and rejects idempotency keys."
    ),
)
def create_incident(
    request: CreateIncidentRequest,
    service: IncidentServiceDependency,
    idempotency_key: Annotated[str | None, Header(pattern=r"^[A-Za-z0-9._:-]{1,128}$", min_length=1, max_length=128)] = None,
) -> IncidentStatusResponse:
    try:
        snapshot = (service.create_incident(request) if idempotency_key is None else
                    service.create_incident(request, idempotency_key=idempotency_key))

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


@router.get("")
def list_incidents(service: IncidentServiceDependency,
                   limit: Annotated[int, Query(ge=1, le=50)] = 20,
                   cursor: str | None = None) -> dict:
    return service.list_metadata(limit=limit, cursor=cursor)


@router.get("/by-idempotency-key/{key}", response_model=IncidentStatusResponse)
def find_by_key(key: str, service: IncidentServiceDependency) -> IncidentStatusResponse:
    try:
        return _response_from_snapshot(service.get_by_idempotency_key(key))
    except IncidentGraphError as error:
        _raise_graph_error(error)


@router.get("/{incident_id}/runs")
def list_runs(incident_id: str, service: IncidentServiceDependency,
              limit: Annotated[int, Query(ge=1, le=50)] = 20,
              cursor: str | None = None) -> dict:
    return service.list_metadata(incident_id=incident_id, limit=limit, cursor=cursor)


@router.get("/{incident_id}/operations")
def list_operations(incident_id: str, service: IncidentServiceDependency) -> dict:
    return service.list_operations(incident_id)


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


@router.post(
    "/{incident_id}/approval",
    response_model=IncidentStatusResponse,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": "The incident does not exist.",
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": (
                "The incident is not awaiting approval or the "
                "decision conflicts with existing state."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "The approval request is invalid.",
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
    summary="Submit an incident approval decision",
    description=(
        "Resumes a workflow paused for human approval and returns "
        "the latest checkpointed state."
    ),
)
def submit_approval(
    incident_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=128,
            pattern=r"^[a-zA-Z0-9-]+$",
        ),
    ],
    request: SubmitApprovalRequest,
    service: IncidentServiceDependency,
) -> IncidentStatusResponse:
    try:
        snapshot = service.submit_approval(
            incident_id,
            request,
        )

    except IncidentNotFoundError as error:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="INCIDENT_NOT_FOUND",
            message="The requested incident was not found.",
        ) from error

    except IncidentNotAwaitingApprovalError as error:
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="INCIDENT_NOT_AWAITING_APPROVAL",
            message=(
                "The incident is not awaiting an approval decision."
            ),
        ) from error

    except IncidentApprovalConflictError as error:
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="APPROVAL_CONFLICT",
            message=(
                "The approval decision conflicts with the current "
                "incident state."
            ),
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
