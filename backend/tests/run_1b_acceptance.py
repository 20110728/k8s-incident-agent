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
    stage3a = database.startswith("incident_agent_test_3a_")
    stage3b = database.startswith("incident_agent_test_3b_")
    stage4a1 = database.startswith("incident_agent_test_4a1_")
    stage4a2 = database.startswith("incident_agent_test_4a2_")
    stage4b1 = database.startswith("incident_agent_test_4b1_")
    stage4b2 = database.startswith("incident_agent_test_4b2_")
    if stage4b1 and environment.get("STAGE4B1_LIVE_MODEL") != "1":
        print("4B-1 requires the live model acceptance wrapper.", file=sys.stderr)
        return 2
    extra = list(STAGE2B_REGRESSION_TARGETS) if database.startswith("incident_agent_test_2b_") or stage3a else []
    if stage3a:
        extra += ["backend/tests/identity"]
    if extra:
        print("Regression scope: operation journal, approval, execution and business verification; not the full historical suite.", flush=True)
    stage2c = database.startswith("incident_agent_test_2c_")
    report = None
    if stage4a1 or stage4a2 or stage4b1 or stage4b2:
        if not environment.get("INCIDENT_AGENT_TEST_AUDIT_DIR"):
            return 2
        report = Path(environment["INCIDENT_AGENT_TEST_AUDIT_DIR"]) / "junit.xml"
        # Make the runner database itself an inspectable migration artifact.
        from backend.app.persistence.database import connect_database
        from backend.app.persistence.settings import DatabaseSettings
        from backend.app.persistence.migrations import run_migrations
        with connect_database(DatabaseSettings(database_url=environment["INCIDENT_AGENT_TEST_DATABASE_URL"])) as connection:
            run_migrations(connection)
    if stage3b:
        if environment.get("STAGE3B_RBAC") != "1" or not environment.get("INCIDENT_AGENT_TEST_AUDIT_DIR"):
            print("3B requires the real credential acceptance wrapper.", file=sys.stderr)
            return 2
        report = Path(environment["INCIDENT_AGENT_TEST_AUDIT_DIR"]) / "junit.xml"
    if stage3a:
        if environment.get("STAGE3A_KIND") != "1" or not environment.get("INCIDENT_AGENT_TEST_AUDIT_DIR"):
            print("3A requires the real kind acceptance wrapper.", file=sys.stderr)
            return 2
        report = Path(environment["INCIDENT_AGENT_TEST_AUDIT_DIR"]) / "junit.xml"
    if stage2c:
        if (environment.get("STAGE2C_RECOVERY") != "1" or environment.get("STAGE2B_KIND") != "1"
                or not environment.get("INCIDENT_AGENT_TEST_AUDIT_DIR")):
            print("2C requires the full process/kind acceptance wrapper.", file=sys.stderr)
            return 2
        report = Path(environment["INCIDENT_AGENT_TEST_AUDIT_DIR"]) / "junit.xml"
    targets = (["backend/tests/persistence", "backend/tests/rbac"] if stage3b else
               ["backend/tests/persistence", "backend/tests/api", "backend/tests/runtime"])
    if stage4a1:
        targets = ["backend/tests/persistence", "backend/tests/messages",
                   "backend/tests/api/test_validation_errors.py",
                   "backend/tests/api/test_incidents_api.py", "backend/tests/api/test_system_api.py"]
    if stage4a2 or stage4b1 or stage4b2:
        targets = ["backend/tests/persistence", "backend/tests/messages", "backend/tests/rounds",
                   "backend/tests/api", "backend/tests/runtime/test_recovery.py",
                   "backend/tests/runtime/test_worker_postgres.py", "backend/tests/runtime/test_operations_postgres.py",
                   "backend/tests/agent/test_approval.py", "backend/tests/agent/test_graph_human_approval.py"]
    if stage4b1:
        targets += ["backend/tests/interactions"]
    if stage4b2:
        targets += ["backend/tests/interactions/test_interactions.py", "backend/tests/dialogue"]
    result = subprocess.call([
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
    ] + targets + extra + (["--junitxml=" + str(report)] if report else []), env=environment)
    if result or not (stage2c or stage3a or stage3b or stage4a1 or stage4a2 or stage4b1 or stage4b2):
        return result
    if stage4b2:
        cases = [case for case in ET.parse(report).iter("testcase")
                 if case.attrib.get("name", "").startswith("test_4B2_")]
        if len(cases) != 11 or any(case.find(tag) is not None for case in cases
                                  for tag in ("skipped", "failure", "error")):
            print("4B-2 question/control/dispatch acceptance incomplete", file=sys.stderr)
            return 1
        print("4B-2: durable questions, stop controls and dispatch arbitration passed.", flush=True)
        return 0
    if stage4b1:
        cases = [case for case in ET.parse(report).iter("testcase")
                 if case.attrib.get("name", "").startswith("test_4B1_")]
        if len(cases) != 9 or any(case.find(tag) is not None for case in cases
                                 for tag in ("skipped", "failure", "error")):
            print("4B-1 database/worker/live-model acceptance incomplete", file=sys.stderr)
            return 1
        print("4B-1: durable routing, historical answers, rechecks and live model passed.", flush=True)
        return 0
    if stage4a2:
        cases = [case for case in ET.parse(report).iter("testcase")
                 if case.attrib.get("name", "").startswith("test_4A2_")]
        if len(cases) != 6 or any(case.find(tag) is not None for case in cases
                                 for tag in ("skipped", "failure", "error")):
            print("4A-2 real round acceptance incomplete", file=sys.stderr)
            return 1
        print("4A-2: round isolation, context, worker recovery and history passed.", flush=True)
        return 0
    if stage4a1:
        cases = [case for case in ET.parse(report).iter("testcase")
                 if case.attrib.get("name", "").startswith("test_4A1_")]
        if len(cases) != 5 or any(case.find(tag) is not None for case in cases
                                 for tag in ("skipped", "failure", "error")):
            print("4A-1 real database/API/process checks incomplete", file=sys.stderr)
            return 1
        print("4A-1: message concurrency, API, history and process persistence passed.", flush=True)
        return 0
    if stage3b:
        cases = [case for case in ET.parse(report).iter("testcase")
                 if case.attrib.get("name", "").startswith("test_3B_real_identity_matrix_and_approved_repair[")]
        if len(cases) != 2 or any(case.find(tag) is not None for case in cases for tag in ("skipped", "failure", "error")):
            print("3B real credential checks incomplete or skipped", file=sys.stderr)
            return 1
        print("3B: reader/remediator real API matrices and approved repair passed.", flush=True)
        return 0
    if stage3a:
        cases = [case for case in ET.parse(report).iter("testcase")
                 if case.attrib.get("name", "").startswith("test_3A_approved_identity_guards_real_write[")]
        if len(cases) != 5 or any(case.find(tag) is not None for case in cases for tag in ("skipped", "failure", "error")):
            print("3A identity scenarios incomplete or skipped", file=sys.stderr)
            return 1
        print("3A: all 5 real identity scenarios passed.", flush=True)
        return 0
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
