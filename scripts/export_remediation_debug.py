"""Export saved plan, approval, execution journal and recovery separately from diagnosis."""
import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import uuid4

from backend.app.persistence.database import connect_database
from backend.app.persistence.settings import get_database_settings
from backend.app.runtime.remediation_export import load_remediation_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--run-id", help="Defaults to latest diagnosis run, even if it has no remediation")
    args = parser.parse_args()
    with connect_database(get_database_settings()) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        report = load_remediation_report(connection, args.incident_id, args.run_id)
    folder = Path("evals/results/remediation-debug")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8] + "-remediation.json")
    with path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(f"run_id: {report['run']['run_id']}")
    print(f"Saved: {path}")
    print("Saved records only; no execution, model or cluster calls. Missing records stay unknown.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        reason = "INCIDENT_OR_RUN_NOT_FOUND" if str(error) == "INCIDENT_OR_RUN_NOT_FOUND" else type(error).__name__
        raise SystemExit("Export failed: " + reason) from None
