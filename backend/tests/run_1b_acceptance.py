"""ECS-only worker acceptance with the URL format shared by production entry points."""
import os
import subprocess
import sys

from backend.app.persistence.settings import get_database_settings
from backend.tests.run_1a_acceptance import isolated_database_url


def main():
    database = os.environ.get("INCIDENT_AGENT_TEST_DATABASE_NAME", "")
    environment = dict(os.environ)
    environment["INCIDENT_AGENT_TEST_DATABASE_URL"] = isolated_database_url(
        get_database_settings().database_url.get_secret_value(), database,
    )
    extra = (["backend/tests/agent", "backend/tests/tools", "backend/tests/service_profiles", "backend/tests/business_recovery"]
             if database.startswith("incident_agent_test_2b_") else [])
    return subprocess.call([
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "backend/tests/persistence", "backend/tests/api", "backend/tests/runtime",
    ] + extra, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
