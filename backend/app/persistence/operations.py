"""Operation journal; every mutation requires the live run owner and epoch."""
from psycopg.types.json import Jsonb

from backend.app.persistence.leases import LeaseRepository, LeaseLost
from backend.app.persistence.runs import RunError, request_digest
from backend.app.runtime.failpoints import hit
from backend.app.runtime.telemetry import report


class ApprovalConflict(RunError):
    status_code = 409
    code = "APPROVAL_CONFLICT"
    message = "The saved approval differs or the run is no longer waiting."


def json_value(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(child) for child in value]
    return value


def approval_binding(state):
    return request_digest({key: json_value(state.get(key)) for key in (
        "request", "remediation_plan", "service_profile", "diagnosis", "evidence",
    )})


class OperationRepository(LeaseRepository):
    def queue_approval(self, run_id, decision, binding):
        payload = {"decision": decision, "binding": binding}
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM incident_agent_app.runs WHERE run_id=%s FOR UPDATE", (run_id,)).fetchone()
            if row is None:
                raise ApprovalConflict()
            if row["approval_payload"] is not None:
                if row["approval_payload"] == payload:
                    return
                raise ApprovalConflict()
            if row["status"] != "waiting_approval":
                raise ApprovalConflict()
            if connection.execute("""SELECT operation_id FROM incident_agent_app.operations
                WHERE incident_id=%s AND state IN ('prepared','dispatching','outcome_unknown','manual_required')""", (row["incident_id"],)).fetchone():
                raise ApprovalConflict()
            connection.execute("""UPDATE incident_agent_app.runs SET approval_payload=%s,status='queued',
                updated_at=clock_timestamp() WHERE run_id=%s""", (Jsonb(payload), run_id))
        report("approval_saved", row, input_value=payload)

    def operation(self, run_id):
        hit("before_operation_read", {"run_id": run_id})
        rows = self._read("SELECT * FROM incident_agent_app.operations WHERE run_id=%s", (run_id,))
        return rows[0] if rows else None

    def history(self, incident_id):
        return self._read("""SELECT operation_id,run_id,approval_id,plan_revision,action,state,
            plan_snapshot,approval_snapshot,before_snapshot,target_snapshot,response_snapshot,observed_snapshot,attribution,error_code,
            created_at,dispatched_at,responded_at,reconciled_at FROM incident_agent_app.operations
            WHERE incident_id=%s ORDER BY created_at DESC LIMIT 50""", (incident_id,))

    def prepare(self, lease, *, approval_id, plan_revision, plan, action, before, target, patch):
        hit("before_operation_prepare", lease)
        operation_id = "op-" + request_digest({"run": lease["run_id"], "plan": plan_revision,
                                               "approval": approval_id, "sequence": 0})
        with self.fence(lease) as connection:
            connection.execute("SELECT incident_id FROM incident_agent_app.incidents WHERE incident_id=%s FOR UPDATE", (lease["incident_id"],))
            if connection.execute("""SELECT operation_id FROM incident_agent_app.operations
                WHERE incident_id=%s AND run_id<>%s AND state IN ('prepared','dispatching','outcome_unknown','manual_required')""",
                (lease["incident_id"], lease["run_id"])).fetchone():
                raise ApprovalConflict()
            existing = connection.execute("SELECT * FROM incident_agent_app.operations WHERE run_id=%s", (lease["run_id"],)).fetchone()
            if existing:
                if existing["operation_id"] != operation_id:
                    raise ApprovalConflict()
                return existing
            return connection.execute("""INSERT INTO incident_agent_app.operations
                (operation_id,run_id,incident_id,approval_id,plan_revision,action,state,lease_epoch,
                 before_snapshot,target_snapshot,request_patch,plan_snapshot,approval_snapshot)
                VALUES (%s,%s,%s,%s,%s,%s,'prepared',%s,%s,%s,%s,%s,%s) RETURNING *""",
                (operation_id, lease["run_id"], lease["incident_id"], approval_id, plan_revision, action,
                 lease["lease_epoch"], Jsonb(before), Jsonb(target), Jsonb(patch), Jsonb(plan),
                 Jsonb(lease["approval_payload"]))).fetchone()

    def dispatch(self, lease, operation_id):
        with self.fence(lease) as connection:
            row = connection.execute("""UPDATE incident_agent_app.operations SET state='dispatching',
                lease_epoch=%s,dispatched_at=clock_timestamp(),updated_at=clock_timestamp()
                WHERE operation_id=%s AND run_id=%s AND state='prepared' RETURNING operation_id""",
                (lease["lease_epoch"], operation_id, lease["run_id"])).fetchone()
            if row is None:
                raise LeaseLost("operation has already been dispatched")

    def record(self, lease, state, *, response=None, observed=None, result=None, code=None, attribution="not_established"):
        hit("before_operation_record", lease)
        if state not in {"succeeded", "rejected", "outcome_unknown", "reconciled", "manual_required"}:
            raise ValueError("invalid operation outcome")
        with self.fence(lease) as connection:
            current = connection.execute("SELECT * FROM incident_agent_app.operations WHERE run_id=%s FOR UPDATE",
                                         (lease["run_id"],)).fetchone()
            allowed = {
                "succeeded": {"dispatching"}, "rejected": {"prepared", "dispatching"},
                "outcome_unknown": {"dispatching"},
                "reconciled": {"succeeded", "reconciled"},
                "manual_required": {"dispatching", "outcome_unknown", "succeeded", "reconciled", "manual_required"},
            }
            if current is None or current["state"] not in allowed[state]:
                raise ValueError("invalid operation transition")
            if ((response is not None and current["response_snapshot"] is not None) or
                    (result is not None and current["result"] is not None)):
                raise ValueError("operation response is immutable")
            connection.execute("""UPDATE incident_agent_app.operations SET state=%s,error_code=%s,
                response_snapshot=COALESCE(%s,response_snapshot),observed_snapshot=COALESCE(%s,observed_snapshot),
                result=COALESCE(%s,result),attribution=%s,updated_at=clock_timestamp(),
                responded_at=CASE WHEN %s::jsonb IS NOT NULL THEN clock_timestamp() ELSE responded_at END,
                reconciled_at=CASE WHEN %s IN ('reconciled','manual_required') THEN clock_timestamp() ELSE reconciled_at END
                WHERE run_id=%s""",
                (state, code, Jsonb(response) if response is not None else None,
                 Jsonb(observed) if observed is not None else None, Jsonb(result) if result is not None else None,
                 attribution, Jsonb(response) if response is not None else None, state, lease["run_id"]))
