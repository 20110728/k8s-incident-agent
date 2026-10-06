"""ECS-only worker acceptance with the URL format shared by production entry points."""
import os
from pathlib import Path
import subprocess
import sys
import json
import xml.etree.ElementTree as ET

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
    stage2c = database.startswith("incident_agent_test_2c_")
    report = None
    if stage2c:
        if (environment.get("STAGE2C_RECOVERY") != "1" or environment.get("STAGE2B_KIND") != "1"
                or not environment.get("INCIDENT_AGENT_TEST_AUDIT_DIR")):
            print("2C requires the full process/kind acceptance wrapper.", file=sys.stderr)
            return 2
        report = Path(environment["INCIDENT_AGENT_TEST_AUDIT_DIR"]) / "junit.xml"
    result = subprocess.call([
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "backend/tests/persistence", "backend/tests/api", "backend/tests/runtime",
    ] + extra + (["--junitxml=" + str(report)] if report else []), env=environment)
    if result or not stage2c:
        return result
    expected = {"R01": 1, "R02": 4, "R03": 1, "R04": 4, "R05": 2, "R06": 3}
    cases = list(ET.parse(report).iter("testcase"))
    counts = {key: sum(case.attrib.get("name", "").startswith("test_" + key + "_")
                      and not any(case.find(tag) is not None for tag in ("skipped", "failure", "error"))
                      for case in cases) for key in expected}
    (report.parent / "recovery-summary.json").write_text(json.dumps(counts, indent=2), encoding="utf-8")
    if counts != expected:
        print(f"2C recovery coverage incomplete: {counts}; expected {expected}", file=sys.stderr)
        return 1
    print("2C R01-R06: all 15 process recovery scenarios passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
