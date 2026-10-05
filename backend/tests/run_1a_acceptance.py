"""ECS entry point: reuse connection settings but require a dedicated test DB."""
import os
import re
import subprocess
import sys

from psycopg.conninfo import make_conninfo

from backend.app.persistence.database import normalize_psycopg_dsn
from backend.app.persistence.settings import get_database_settings


def main():
    database = os.environ.get("INCIDENT_AGENT_TEST_DATABASE_NAME", "")
    if not re.fullmatch(r"incident_agent_test_[a-z0-9_]+", database):
        raise SystemExit("Set INCIDENT_AGENT_TEST_DATABASE_NAME to a dedicated incident_agent_test_* database")
    original = get_database_settings().database_url.get_secret_value()
    environment = dict(os.environ)
    environment["INCIDENT_AGENT_TEST_DATABASE_URL"] = make_conninfo(normalize_psycopg_dsn(original), dbname=database)
    # Keep secrets out of command arguments and stdout. Never change PGVECTOR_URL.
    return subprocess.call([
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "backend/tests/persistence", "backend/tests/api",
    ], env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
