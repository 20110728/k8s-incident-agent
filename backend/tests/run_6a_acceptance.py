"""Isolated ECS acceptance: PostgreSQL, controlled model/Kubernetes, no live writes."""
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

from backend.app.persistence.database import connect_database
from backend.app.persistence.migrations import run_migrations
from backend.app.persistence.settings import DatabaseSettings, get_database_settings
from backend.tests.run_1a_acceptance import isolated_database_url


def main():
    database = os.environ.get("INCIDENT_AGENT_TEST_DATABASE_NAME", "")
    audit = os.environ.get("INCIDENT_AGENT_TEST_AUDIT_DIR")
    if not database.startswith("incident_agent_test_6a_") or not audit:
        raise ValueError("Use bash scripts/accept_stage6a.sh")
    environment = dict(os.environ)
    url = isolated_database_url(get_database_settings().database_url.get_secret_value(), database)
    environment["INCIDENT_AGENT_TEST_DATABASE_URL"] = url
    with connect_database(DatabaseSettings(database_url=url)) as connection:
        run_migrations(connection)
    report = Path(audit) / "junit.xml"
    targets = ["backend/tests/investigation", "backend/tests/persistence/test_migrations.py",
        "backend/tests/interactions", "backend/tests/delayed", "backend/tests/dialogue",
        "backend/tests/llm", "backend/tests/rag", "backend/tests/observations",
        "backend/tests/runtime/test_operation_protocol.py", "backend/tests/runtime/test_operations_postgres.py"]
    result = subprocess.call([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                              *targets, "--junitxml=" + str(report)], env=environment)
    if result:
        return result
    cases = list(ET.parse(report).iter("testcase"))
    if not cases or any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
        print("Acceptance incomplete: skipped/failed tests", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
