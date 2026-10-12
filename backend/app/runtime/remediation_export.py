"""Read saved remediation records only; no graph replay or live resource access."""
import json

from backend.app.tools.investigation import redact_output


def remediation_report(row, operation):
    snapshot = row.get("output_snapshot") or {}
    if operation and (operation["incident_id"] != row["incident_id"] or operation["run_id"] != row["run_id"]):
        raise ValueError("OPERATION_RUN_MISMATCH")
    steps = {"plan_remediation", "prepare_approval", "request_human_approval", "execute_remediation", "verify_recovery"}
    report = {
        "report_version": "remediation-v1",
        "source": "saved_records_only_no_model_cluster_or_execution_calls",
        "run": {k: row.get(k) for k in ("incident_id", "run_id", "workflow_version", "status", "created_at", "updated_at")},
        "phase": snapshot.get("phase"),
        "remediation_plan": snapshot.get("remediation_plan"),
        "approval": {k: snapshot.get(k) for k in (
            "requires_approval", "approved", "approval_status", "approval_request", "approval_record")},
        "saved_approval_payload": row.get("approval_payload"),
        "action_result": snapshot.get("action_result"),
        "verification_result": snapshot.get("verification_result"),
        "operation": {k: operation.get(k) for k in (
            "operation_id", "incident_id", "run_id", "approval_id", "plan_revision", "action", "state",
            "plan_snapshot", "approval_snapshot", "before_snapshot", "target_snapshot", "request_patch",
            "response_snapshot", "observed_snapshot", "result", "attribution", "error_code",
            "created_at", "dispatched_at", "responded_at", "reconciled_at", "updated_at",
        )} if operation else None,
        "trace": [item for item in snapshot.get("trace", []) if item.get("step") in steps],
        "errors": [item for item in snapshot.get("errors", []) if item.get("stage") in steps],
        "availability": {"published_plan": snapshot.get("remediation_plan") is not None,
                         "operation_journal": operation is not None,
                         "published_verification": snapshot.get("verification_result") is not None},
        "limits": [
            "Run succeeded means workflow completion, not proof of execution or recovery.",
            "Null means no saved record in this source; never infer that a write did or did not occur.",
            "Published snapshots can lag the operation journal while a run is active.",
            "Operation records contain saved dispatch/response/reconciliation snapshots, not full process stdout or Kubernetes audit logs.",
            "Verification includes only saved samples and their stated scope; missing historical samples cannot be reconstructed.",
        ],
    }
    return json.loads(redact_output(report))


def load_remediation_report(connection, incident_id, run_id=None):
    condition = " AND run_id=%s" if run_id else ""
    params = (incident_id, run_id) if run_id else (incident_id,)
    row = connection.execute(
        "SELECT incident_id,run_id,workflow_version,status,created_at,updated_at,approval_payload,output_snapshot "
        "FROM incident_agent_app.runs WHERE incident_id=%s AND run_kind='diagnosis'" + condition +
        " ORDER BY created_at DESC,run_id DESC LIMIT 1", params).fetchone()
    if row is None:
        raise ValueError("INCIDENT_OR_RUN_NOT_FOUND")
    operation = connection.execute(
        "SELECT * FROM incident_agent_app.operations WHERE incident_id=%s AND run_id=%s",
        (row["incident_id"], row["run_id"])).fetchone()
    return remediation_report(row, operation)
