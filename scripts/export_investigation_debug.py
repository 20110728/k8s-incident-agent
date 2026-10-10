"""Export one saved investigation; never replay the graph or contact the model/cluster."""
import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import uuid4

from backend.app.persistence.database import connect_database
from backend.app.persistence.settings import get_database_settings
from backend.app.investigation.diagnostics import debug_report
from backend.app.investigation.cards import saved_cards
from backend.app.investigation.brief_debug import brief_debug_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--run-id", help="Defaults to the latest diagnosis run of this incident")
    parser.add_argument("--evidence-cards", action="store_true", help="Include bounded program-extracted saved evidence cards (no model calls)")
    parser.add_argument("--brief", action="store_true", help="Compact troubleshooting report including evidence parsing status; overrides --evidence-cards")
    args = parser.parse_args()
    with connect_database(get_database_settings()) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        condition = " AND run_id=%s" if args.run_id else ""
        params = (args.incident_id, args.run_id) if args.run_id else (args.incident_id,)
        row = connection.execute("SELECT incident_id,run_id,workflow_version,status,created_at,updated_at,output_snapshot "
            "FROM incident_agent_app.runs WHERE incident_id=%s AND run_kind='diagnosis'" + condition +
            " ORDER BY created_at DESC,run_id DESC LIMIT 1", params).fetchone()
        if row is None:
            raise ValueError("INCIDENT_OR_RUN_NOT_FOUND")
        budget = connection.execute("SELECT payload FROM incident_agent_app.run_budgets WHERE run_id=%s", (row["run_id"],)).fetchone()
        report = (brief_debug_report if args.brief else debug_report)(row, budget["payload"] if budget else {})
        if args.evidence_cards and not args.brief:
            report["evidence_cards"] = saved_cards(row.get("output_snapshot"), budget["payload"] if budget else {})
    folder = Path("evals/results/investigation-debug")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8] + ("-brief.json" if args.brief else ".json"))
    with path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(f"run_id: {row['run_id']}")
    print(f"Saved: {path}")
    print("Saved records only; no model/tool calls. Older missing diagnostics remain unavailable.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Avoid printing connection strings/provider secrets in operational errors.
        reason = "INCIDENT_OR_RUN_NOT_FOUND" if str(error) == "INCIDENT_OR_RUN_NOT_FOUND" else type(error).__name__
        raise SystemExit("Export failed: " + reason) from None
