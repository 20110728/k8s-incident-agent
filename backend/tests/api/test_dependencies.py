from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from backend.app.api import dependencies


def test_incident_service_context_owns_resources(
    monkeypatch,
) -> None:
    settings = object()
    connection = object()
    saver = object()
    service = object()
    events: list[str] = []

    @contextmanager
    def fake_connect_database(
        received_settings,
    ) -> Iterator[object]:
        assert received_settings is settings
        events.append("database_entered")
        try:
            yield connection
        finally:
            events.append("database_exited")

    def fake_run_migrations(
        received_connection,
    ) -> list[int]:
        assert received_connection is connection
        events.append("migrations_run")
        return []

    @contextmanager
    def fake_postgres_checkpointer(
        received_settings,
    ) -> Iterator[object]:
        assert received_settings is settings
        events.append("checkpointer_entered")
        try:
            yield saver
        finally:
            events.append("checkpointer_exited")

    def fake_build_incident_service(
        *,
        checkpointer,
        repository,
    ):
        assert checkpointer is saver
        assert repository is not None
        events.append("service_built")
        return service

    monkeypatch.setattr(
        dependencies,
        "get_database_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        dependencies,
        "connect_database",
        fake_connect_database,
    )
    monkeypatch.setattr(
        dependencies,
        "run_migrations",
        fake_run_migrations,
    )
    monkeypatch.setattr(
        dependencies,
        "postgres_checkpointer",
        fake_postgres_checkpointer,
    )
    monkeypatch.setattr(
        dependencies,
        "build_incident_service",
        fake_build_incident_service,
    )

    with dependencies.incident_service_context() as result:
        assert result is service
        events.append("service_used")

    assert events == [
        "database_entered",
        "migrations_run",
        "database_exited",
        "checkpointer_entered",
        "service_built",
        "service_used",
        "checkpointer_exited",
    ]