"""ECS-only worker acceptance with the URL format shared by production entry points."""
import os
import subprocess
import sys

from backend.app.persistence.settings import get_database_settings
from backend.tests.run_1a_acceptance import isolated_database_url


# Scope by changed behavior, not by whole historical test directories. The
# runtime suite below always includes the complete 2B journal and kind tests.
STAGE2B_REGRESSION_TARGETS = (
    "backend/tests/agent/test_schemas.py",
    "backend/tests/agent/test_approval.py",
    "backend/tests/agent/test_execution_policy.py",
    "backend/tests/agent/test_executor_and_node.py",
    "backend/tests/agent/test_graph_approval_routing.py",
    "backend/tests/agent/test_graph_human_approval.py",
    "backend/tests/agent/test_graph_execution.py",
    "backend/tests/agent/test_verification.py",
    "backend/tests/tools/test_remediation_tools.py",
    "backend/tests/business_recovery",
)


def main():
    database = os.environ.get("INCIDENT_AGENT_TEST_DATABASE_NAME", "")
    environment = dict(os.environ)
    environment["INCIDENT_AGENT_TEST_DATABASE_URL"] = isolated_database_url(
        get_database_settings().database_url.get_secret_value(), database,
    )
    extra = list(STAGE2B_REGRESSION_TARGETS) if database.startswith("incident_agent_test_2b_") else []
    if extra:
        print("2B scope: operation journal, approval, execution and business verification; not the full historical suite.", flush=True)
    return subprocess.call([
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "backend/tests/persistence", "backend/tests/api", "backend/tests/runtime",
    ] + extra, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
