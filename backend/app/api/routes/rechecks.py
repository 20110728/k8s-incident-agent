"""Read-only Kubernetes recheck endpoints; never invoke or resume a graph."""

from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from backend.app.api.dependencies import get_incident_service
from backend.app.api.errors import ApiError
from backend.app.agent.dependencies import build_kubernetes_collector
from backend.app.persistence.database import connect_database
from backend.app.persistence.settings import get_database_settings
from backend.app.persistence.rechecks import (
    PostgresRecheckRepository,
    RecheckRepositoryError,
)
from backend.app.services.incident_service import (
    IncidentNotFoundError,
    IncidentServiceError,
)
from backend.app.services.recheck_service import (
    IncidentRecheckService,
    RecheckRequest,
    RecheckResult,
    RecheckUnavailable,
)

router = APIRouter(prefix="/incidents", tags=["rechecks"])
IncidentId = Annotated[
    str, Path(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9-]+$")
]


def get_recheck_service(incidents=Depends(get_incident_service)):
    # Building a service for a GET must not even initialize a Kubernetes client.
    class LazyCollector:
        def collect(self, namespace, service_name):
            return build_kubernetes_collector().collect(namespace, service_name)

    return IncidentRecheckService(
        incidents,
        LazyCollector(),
        PostgresRecheckRepository(partial(connect_database, get_database_settings())),
    )


def perform(operation):
    # Cluster observation failures become persisted unknown results in the
    # service; unavailable storage or checkpoints fail the HTTP request instead.
    try:
        return operation()
    except IncidentNotFoundError as error:
        raise ApiError(
            status_code=404, code="INCIDENT_NOT_FOUND", message="Incident not found."
        ) from error
    except RecheckUnavailable as error:
        raise ApiError(
            status_code=409, code="RECHECK_NOT_AVAILABLE", message=str(error)
        ) from error
    except (IncidentServiceError, RecheckRepositoryError) as error:
        raise ApiError(
            status_code=503,
            code="RECHECK_SERVICE_UNAVAILABLE",
            message="Could not load the incident or persist/read its recheck.",
        ) from error


@router.post("/{incident_id}/rechecks", response_model=RecheckResult, status_code=201)
def create_recheck(
    incident_id: IncidentId,
    request: RecheckRequest,
    service=Depends(get_recheck_service),
):
    return perform(lambda: service.create(incident_id, request))


@router.get("/{incident_id}/rechecks")
def list_rechecks(
    incident_id: IncidentId,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    before_sequence: Annotated[int | None, Query(ge=1)] = None,
    service=Depends(get_recheck_service),
):
    return perform(lambda: service.history(incident_id, limit, before_sequence))
