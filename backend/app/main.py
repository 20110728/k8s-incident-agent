from __future__ import annotations

from collections.abc import Callable
from contextlib import (
    AbstractContextManager,
    asynccontextmanager,
)

from fastapi import FastAPI

from backend.app.persistence.serialization_security import (
    STRICT_MSGPACK_ENABLED,
)
from backend.app.api.dependencies import (
    incident_service_context,
)
from backend.app.api.errors import register_exception_handlers
from backend.app.api.routes.incidents import (
    router as incident_router,
)
from backend.app.api.routes.system import (
    router as system_router,
)
from backend.app.config import ApiSettings, get_api_settings
from backend.app.services.incident_service import (
    IncidentApplicationService,
)

from fastapi.middleware.cors import CORSMiddleware

IncidentServiceContextFactory = Callable[
    [],
    AbstractContextManager[IncidentApplicationService],
]


def create_app(
    settings: ApiSettings | None = None,
    *,
    service_context_factory: (
        IncidentServiceContextFactory | None
    ) = None,
) -> FastAPI:
    resolved_settings = settings or get_api_settings()

    docs_url = (
        "/docs"
        if resolved_settings.docs_enabled
        else None
    )
    redoc_url = (
        "/redoc"
        if resolved_settings.docs_enabled
        else None
    )
    openapi_url = (
        "/openapi.json"
        if resolved_settings.docs_enabled
        else None
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if (
            resolved_settings.environment == "test"
            and service_context_factory is None
        ):
            yield
            return

        factory = (
            service_context_factory
            or incident_service_context
        )
        application.state.ready = False

        with factory() as service:
            application.state.incident_service = service
            application.state.ready = True

            try:
                yield
            finally:
                application.state.ready = False
                del application.state.incident_service

    app = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        description=(
            "API for the allowlisted Kubernetes "
            "incident diagnosis and remediation workflow."
        ),
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )

    app.state.settings = resolved_settings
    app.state.strict_msgpack = STRICT_MSGPACK_ENABLED
    app.state.ready = (
        resolved_settings.environment == "test"
    )

    if resolved_settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(
                resolved_settings.cors_allowed_origins
            ),
            allow_credentials=False,
            allow_methods=[
                "GET",
                "POST",
                "OPTIONS",
            ],
            allow_headers=[
                "Accept",
                "Content-Type",
            ],
            max_age=600,
        )

    register_exception_handlers(app)
    app.include_router(system_router)
    app.include_router(
        incident_router,
        prefix=resolved_settings.api_prefix,
    )

    return app


app = create_app()