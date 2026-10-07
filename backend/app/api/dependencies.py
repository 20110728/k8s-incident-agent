from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial

from fastapi import Request
from backend.app.config import get_api_settings
from backend.app.persistence.runs import PostgresRunRepository, QueuedExecutionUnavailable
from backend.app.persistence.operations import OperationRepository

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
    runs: PostgresRunRepository | None = None,
    execution_mode: str = "sync",
) -> IncidentApplicationService:
    if execution_mode == "queued":
        disabled = _DisabledWorkflowDependency()
        graph = build_incident_graph(
            collector=disabled, retriever=disabled, diagnoser=disabled,
            planner=disabled, executor=disabled, verifier=disabled,
            checkpointer=checkpointer,
            dialogue=True,
        )
        return IncidentApplicationService(
            _ReadOnlyGraph(graph), repository, runs=runs, execution_mode=execution_mode,
        )
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
        runs=runs,
        execution_mode=execution_mode,
    )


class _DisabledWorkflowDependency:
    def __getattr__(self, name):
        def unavailable(*args, **kwargs):
            raise QueuedExecutionUnavailable()
        return unavailable


class _ReadOnlyGraph:
    def __init__(self, graph):
        self._graph = graph

    def get_state(self, config):
        return self._graph.get_state(config)

    def invoke(self, *args, **kwargs):
        raise QueuedExecutionUnavailable()


@contextmanager
def incident_service_context(*, execution_mode: str | None = None) -> (
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
            runs=OperationRepository(connection_factory),
            execution_mode=execution_mode or get_api_settings().execution_mode,
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
