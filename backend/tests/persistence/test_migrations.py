from backend.app.persistence.migrations import (
    MIGRATIONS,
    run_migrations,
)
from backend.tests.persistence.fakes import (
    FakeMigrationConnection,
)


def test_run_migrations_applies_pending_version() -> None:
    connection = FakeMigrationConnection()

    applied = run_migrations(connection)

    assert applied == [1, 2]
    assert connection.transaction_value.enter_count == 1
    assert connection.transaction_value.exit_count == 1

    calls = connection.cursor_value.calls
    assert "CREATE SCHEMA IF NOT EXISTS" in calls[0]["query"]
    assert "schema_migrations" in calls[1]["query"]
    assert "SELECT version" in calls[2]["query"]
    assert "CREATE TABLE incident_agent_app.incidents" in (
        calls[3]["query"]
    )
    assert "CREATE INDEX incidents_created_at_idx" in (
        calls[4]["query"]
    )
    assert calls[5]["params"] == (
        MIGRATIONS[0].version,
        MIGRATIONS[0].name,
    )


def test_run_migrations_skips_applied_version() -> None:
    connection = FakeMigrationConnection(
        applied_versions=[1, 2]
    )

    applied = run_migrations(connection)

    assert applied == []
    assert len(connection.cursor_value.calls) == 3


def test_existing_database_adds_rechecks_without_recreating_incidents():
    connection = FakeMigrationConnection(applied_versions=[1])
    assert run_migrations(connection) == [2]
    queries = [call['query'] for call in connection.cursor_value.calls]
    assert any('CREATE TABLE incident_agent_app.rechecks' in query for query in queries)
    assert not any('CREATE TABLE incident_agent_app.incidents' in query for query in queries)
