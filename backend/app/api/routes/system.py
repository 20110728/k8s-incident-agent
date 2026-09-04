from fastapi import APIRouter, Request, status

from backend.app.api.errors import ApiError
from backend.app.api.schemas import (
    ErrorResponse,
    HealthResponse,
    ReadinessResponse,
)
from backend.app.config import ApiSettings


router = APIRouter(tags=["system"])


def _settings(request: Request) -> ApiSettings:
    return request.app.state.settings


@router.get(
    "/healthz",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Check API process health",
    description=(
        "Returns success when the FastAPI process is running. "
        "It does not call Kubernetes, PostgreSQL, or the LLM."
    ),
)
def health(request: Request) -> HealthResponse:
    settings = _settings(request)

    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
    )


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "API startup is incomplete.",
        }
    },
    summary="Check API readiness",
    description=(
        "Returns success after application bootstrap. "
        "External dependency checks are added separately."
    ),
)
def readiness(request: Request) -> ReadinessResponse:
    ready = bool(
        getattr(
            request.app.state,
            "ready",
            False,
        )
    )

    if not ready:
        raise ApiError(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            code="SERVICE_NOT_READY",
            message="The API is not ready.",
            details={
                "checks": {
                    "api": False,
                }
            },
        )

    return ReadinessResponse(
        status="ready",
        checks={
            "api": True,
        },
    )