"""Explicit new diagnosis rounds; never resume an old graph or approval."""
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from backend.app.persistence.operations import OperationRepository, json_value
from backend.app.persistence.runs import RunError, request_digest, validate_key
from backend.app.services.round_context import build_round_context, selected_workflow


class RoundConflict(RunError):
    status_code = 409
    code = "ROUND_NOT_AVAILABLE"
    message = "The incident changed, is still active, or has an unresolved operation."


class RoundNotFound(RunError):
    status_code = 404
    code = "ROUND_INPUT_NOT_FOUND"
    message = "The incident, user message or run was not found."


class RoundRepository(OperationRepository):
    def by_round_key(self, incident_id, key):
        validate_key(key)
        rows = self._read("SELECT * FROM incident_agent_app.runs WHERE idempotency_scope=%s AND idempotency_key=%s",
                          ("incident-round:" + incident_id, key))
        return rows[0] if rows else None

    def get_round(self, incident_id, run_id):
        rows = self._read("SELECT * FROM incident_agent_app.runs WHERE incident_id=%s AND run_id=%s", (incident_id, run_id))
        if not rows:
            raise RoundNotFound()
        return rows[0]

    def accept_round(self, incident_id, message_id, key, parent_run_id, previous):
        validate_key(key)
        digest = request_digest({"message_id": message_id})
        try:
            with self._connect() as conn:
                incident = conn.execute("SELECT * FROM incident_agent_app.incidents WHERE incident_id=%s FOR UPDATE",
                                        (incident_id,)).fetchone()
                if not incident:
                    raise RoundNotFound()
                replay = conn.execute("SELECT * FROM incident_agent_app.runs WHERE idempotency_scope=%s AND idempotency_key=%s",
                                      ("incident-round:" + incident_id, key)).fetchone()
                if replay:
                    return self._replay(replay, digest)
                latest = conn.execute("SELECT * FROM incident_agent_app.runs WHERE incident_id=%s AND run_kind='diagnosis' ORDER BY created_at DESC,run_id DESC LIMIT 1",
                                      (incident_id,)).fetchone()
                if (latest["run_id"] if latest else None) != parent_run_id:
                    raise RoundConflict()
                if conn.execute("""SELECT 1 FROM incident_agent_app.runs WHERE incident_id=%s
                    AND run_kind='interaction' AND status NOT IN ('succeeded','failed','cancelled')""", (incident_id,)).fetchone():
                    raise RoundConflict()
                invalidated = latest and latest["status"] == "cancelled" and latest.get("invalidated_at")
                if (latest and latest["status"] not in {"succeeded", "failed", "cancelled"}) or (
                    (not invalidated and (previous.get("phase") == "awaiting_approval" or previous.get("approval_status") == "pending"))
                ):
                    raise RoundConflict()
                if not latest and previous.get("approved") and previous.get("phase") != "verification_succeeded":
                    raise RoundConflict()  # Unjournaled legacy write must not be bypassed.
                if not latest:
                    phase = str(previous.get("phase") or "")
                    terminal = phase in {"failed", "remediation_skipped", "approval_rejected", "verification_succeeded"} or phase.endswith("_failed")
                    terminal = terminal or (phase == "remediation_planned" and not previous.get("requires_approval"))
                    if not terminal:
                        raise RoundConflict()  # Unknown/in-progress legacy state is read-only.
                if conn.execute("""SELECT 1 FROM incident_agent_app.operations WHERE incident_id=%s
                    AND state IN ('prepared','dispatching','outcome_unknown','manual_required') LIMIT 1""", (incident_id,)).fetchone():
                    raise RoundConflict()
                message = conn.execute("SELECT * FROM incident_agent_app.messages WHERE incident_id=%s AND message_id=%s AND role='user'",
                                       (incident_id, message_id)).fetchone()
                if not message:
                    raise RoundNotFound()
                if latest and message["sequence"] <= (latest.get("input_message_sequence") or 0):
                    raise RoundConflict()
                if conn.execute("SELECT 1 FROM incident_agent_app.runs WHERE source_message_id=%s", (message_id,)).fetchone():
                    raise RoundConflict()
                messages = conn.execute("""SELECT * FROM incident_agent_app.messages WHERE incident_id=%s AND sequence<=%s
                    AND role='user' ORDER BY sequence DESC LIMIT 11""", (incident_id, message["sequence"])).fetchall()
                context = build_round_context(messages, previous, parent_run_id, incident["thread_id"])
                payload = {key: incident[key] for key in ("namespace", "service_name", "description")}
                run_id, thread_id = str(uuid4()), str(uuid4())
                row = conn.execute("""INSERT INTO incident_agent_app.runs
                    (run_id,incident_id,thread_id,parent_run_id,input_revision,input_payload,input_sha256,
                     workflow_version,idempotency_scope,idempotency_key,request_sha256,
                     source_message_id,input_message_sequence,context_snapshot,context_sha256,event_revision)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (run_id, incident_id, thread_id, parent_run_id,
                     latest["input_revision"] + 1 if latest else 1, Jsonb(payload), request_digest(payload), selected_workflow(),
                     "incident-round:" + incident_id, key, digest, message_id, message["sequence"],
                     Jsonb(context), request_digest(context), incident["event_revision"])).fetchone()
                # Old terminal output is frozen before the new run becomes visible.
                if latest and latest["output_snapshot"] is None:
                    conn.execute("UPDATE incident_agent_app.runs SET output_snapshot=%s WHERE run_id=%s",
                                 (Jsonb(json_value(previous)), parent_run_id))
                conn.execute("UPDATE incident_agent_app.incidents SET phase='created',updated_at=clock_timestamp() WHERE incident_id=%s",
                             (incident_id,))
            return row
        except psycopg.Error as error:
            raise RunError() from error
