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

    assert applied == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    assert connection.transaction_value.enter_count == 1
    assert connection.transaction_value.exit_count == 1

    assert "pg_advisory_xact_lock" in connection.cursor_value.calls[0]["query"]
    calls = connection.cursor_value.calls[1:]
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
        applied_versions=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    )

    applied = run_migrations(connection)

    assert applied == []
    assert len(connection.cursor_value.calls) == 4


def test_existing_database_adds_rechecks_without_recreating_incidents():
    connection = FakeMigrationConnection(applied_versions=[1])
    assert run_migrations(connection) == [2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    queries = [call['query'] for call in connection.cursor_value.calls]
    assert any('CREATE TABLE incident_agent_app.rechecks' in query for query in queries)
    assert not any('CREATE TABLE incident_agent_app.incidents' in query for query in queries)


def test_version_two_only_adds_runs():
    connection = FakeMigrationConnection(applied_versions=[1, 2])
    assert run_migrations(connection) == [3, 4, 5, 6, 7, 8, 9, 10, 11]
    queries = [call['query'] for call in connection.cursor_value.calls]
    assert any('CREATE TABLE incident_agent_app.runs' in query for query in queries)
    assert not any('CREATE TABLE incident_agent_app.incidents' in query for query in queries)
    assert not any('CREATE TABLE incident_agent_app.rechecks' in query for query in queries)


def test_existing_worker_database_adds_conservative_checkpoint_marker():
    connection = FakeMigrationConnection(applied_versions=[1, 2, 3, 4])
    assert run_migrations(connection) == [5, 6, 7, 8, 9, 10, 11]
    queries = [call['query'] for call in connection.cursor_value.calls]
    assert any('ADD COLUMN checkpoint_started' in query for query in queries)
    assert any('checkpoint_started=TRUE WHERE attempt>0' in query for query in queries)


def test_version_five_only_adds_operation_ledger_and_durable_approval():
    connection = FakeMigrationConnection(applied_versions=[1, 2, 3, 4, 5])
    assert run_migrations(connection) == [6, 7, 8, 9, 10, 11]
    queries = [call['query'] for call in connection.cursor_value.calls]
    assert any('ADD COLUMN approval_payload' in query for query in queries)
    assert any('CREATE TABLE incident_agent_app.operations' in query for query in queries)
    assert not any('CREATE TABLE incident_agent_app.runs' in query for query in queries)


def test_version_six_adds_messages_without_rewriting_history():
    connection = FakeMigrationConnection(applied_versions=[1, 2, 3, 4, 5, 6])
    assert run_migrations(connection) == [7, 8, 9, 10, 11]
    queries = [call['query'] for call in connection.cursor_value.calls]
    assert any('CREATE TABLE incident_agent_app.messages' in query for query in queries)
    assert not any('UPDATE incident_agent_app.' in query for query in queries)


def test_version_seven_adds_round_fields_without_rewriting_history():
    connection = FakeMigrationConnection(applied_versions=[1, 2, 3, 4, 5, 6, 7])
    assert run_migrations(connection) == [8, 9, 10, 11]
    queries = [call['query'] for call in connection.cursor_value.calls]
    assert any('ADD COLUMN context_snapshot' in query for query in queries)
    assert any('ADD COLUMN output_snapshot' in query for query in queries)
    assert not any('UPDATE incident_agent_app.' in query for query in queries)


def test_version_eight_adds_interactions_without_rewriting_results():
    connection = FakeMigrationConnection(applied_versions=list(range(1, 9)))
    assert run_migrations(connection) == [9, 10, 11]
    queries = [call['query'] for call in connection.cursor_value.calls]
    assert any('ADD COLUMN interaction_progress' in query for query in queries)
    assert any('runs(incident_id,run_kind)' in query for query in queries)
    assert any('rechecks ADD COLUMN run_id TEXT UNIQUE' in query for query in queries)
    assert not any('UPDATE incident_agent_app.' in query or 'DELETE FROM' in query for query in queries)


def test_version_nine_adds_control_metadata_without_rewriting_checkpoints():
    connection = FakeMigrationConnection(applied_versions=list(range(1, 10)))
    assert run_migrations(connection) == [10, 11]
    queries = [call['query'] for call in connection.cursor_value.calls]
    assert any('CREATE TABLE incident_agent_app.controls' in query for query in queries)
    assert any('ADD COLUMN question_payload' in query for query in queries)
    assert any('ADD COLUMN event_revision' in query for query in queries)
    assert not any('UPDATE ' in query or 'DELETE ' in query for query in queries)
