from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial

from fastapi import Request

from backend.app.agent.dependencies import (
    build_diagnosis_service,
    build_kubernetes_collector,
    build_recovery_verifier,
    build_remediation_executor,
    build_remediation_planner,
    build_runbook_retriever,
)
from backend.app.agent.graph import build_incident_graph
from backend.app.persistence.checkpointer import (
    postgres_checkpointer,
)
from backend.app.persistence.database import connect_database
from backend.app.persistence.incidents import (
    IncidentRepositoryPort,
    PostgresIncidentRepository,
)
from backend.app.persistence.migrations import run_migrations
from backend.app.persistence.settings import (
    get_database_settings,
)
from backend.app.services.incident_service import (
    IncidentApplicationService,
)


def build_incident_service(
    *,
    checkpointer: object,
    repository: IncidentRepositoryPort,
) -> IncidentApplicationService:
    graph = build_incident_graph(
        collector=build_kubernetes_collector(),
        retriever=build_runbook_retriever(),
        diagnoser=build_diagnosis_service(),
        planner=build_remediation_planner(),
        executor=build_remediation_executor(),
        verifier=build_recovery_verifier(),
        checkpointer=checkpointer,
    )

    return IncidentApplicationService(
        graph,
        repository,
    )


@contextmanager
def incident_service_context() -> (
    Iterator[IncidentApplicationService]
):
    settings = get_database_settings()

    with connect_database(settings) as connection:
        run_migrations(connection)

    connection_factory = partial(
        connect_database,
        settings,
    )
    repository = PostgresIncidentRepository(
        connection_factory
    )

    with postgres_checkpointer(settings) as checkpointer:
        yield build_incident_service(
            checkpointer=checkpointer,
            repository=repository,
        )


def get_incident_service(
    request: Request,
) -> IncidentApplicationService:
    service = getattr(
        request.app.state,
        "incident_service",
        None,
    )

    if service is None:
        raise RuntimeError(
            "incident service is not initialized"
        )

    return service