"""Regression for the URL shared by real DB, checkpoint, and child-process tests."""
import pytest
from psycopg.conninfo import conninfo_to_dict

from backend.app.persistence.database import normalize_psycopg_dsn
from backend.tests.run_1a_acceptance import isolated_database_url


@pytest.mark.parametrize("scheme", ["postgresql+psycopg", "postgres+psycopg", "postgresql", "postgres"])
def test_isolated_url_preserves_credentials_and_options(scheme):
    original = f"{scheme}://demo:p%40ss%3Aword@127.0.0.1:5433/demo?sslmode=require&dbname=production"
    result = isolated_database_url(original, "incident_agent_test_1a_regression")
    assert normalize_psycopg_dsn(result) == result
    parsed = conninfo_to_dict(result)
    assert parsed["dbname"] == "incident_agent_test_1a_regression"
    assert parsed["user"] == "demo" and parsed["password"] == "p@ss:word"
    assert parsed["host"] == "127.0.0.1" and parsed["port"] == "5433"
    assert parsed["sslmode"] == "require"


@pytest.mark.parametrize("database", ["", "production", "incident_agent_test_a/other", "incident_agent_test_a?dbname=production"])
def test_isolated_url_rejects_unsafe_database_name(database):
    with pytest.raises(ValueError):
        isolated_database_url("postgresql://demo:example@localhost/demo", database)
