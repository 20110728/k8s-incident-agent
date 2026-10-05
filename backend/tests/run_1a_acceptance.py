"""ECS entry point: reuse connection settings but require a dedicated test DB."""
import os
import re
import subprocess
import sys

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend.app.persistence.database import normalize_psycopg_dsn
from backend.app.persistence.settings import get_database_settings


def isolated_database_url(original: str, database: str) -> str:
    """Keep URI format for both Psycopg and application/checkpointer entry points."""
    if not re.fullmatch(r"incident_agent_test_[a-z0-9_]+", database):
        raise ValueError("Set INCIDENT_AGENT_TEST_DATABASE_NAME to a dedicated incident_agent_test_* database")
    parts = urlsplit(normalize_psycopg_dsn(original))
    # A query dbname would override the path; remove it to preserve isolation.
    query = urlencode([
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "dbname"
    ])
    return urlunsplit((parts.scheme, parts.netloc, "/" + database, query, ""))


def main():
    database = os.environ.get("INCIDENT_AGENT_TEST_DATABASE_NAME", "")
    original = get_database_settings().database_url.get_secret_value()
    environment = dict(os.environ)
    environment["INCIDENT_AGENT_TEST_DATABASE_URL"] = isolated_database_url(original, database)
    # Keep secrets out of command arguments and stdout. Never change PGVECTOR_URL.
    return subprocess.call([
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "backend/tests/persistence", "backend/tests/api",
    ], env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
