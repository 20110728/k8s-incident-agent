"""Atomic interaction acceptance and lease-fenced publication."""
from contextlib import nullcontext
from uuid import uuid4

import psycopg
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from backend.app.persistence.messages import PostgresMessageRepository
from backend.app.persistence.rounds import RoundRepository, RoundConflict, RoundNotFound
from backend.app.persistence.runs import RunError, IdempotencyConflict, request_digest, run_summary
from backend.app.services.message_schemas import MessageDraft
from backend.app.services.interaction_schemas import INTERACTION_WORKFLOW
from backend.app.services.recheck_service import TERMINAL_PHASES

TERMINAL = {"succeeded", "failed", "cancelled"}


class InteractionRepository(RoundRepository):
    def by_interaction_key(self, incident_id, key):
        rows = self._read("SELECT * FROM incident_agent_app.runs WHERE idempotency_scope=%s AND idempotency_key=%s",
                          ("interaction:" + incident_id, key))
        return rows[0] if rows else None

    def status(self, incident_id):
        rows = self._read("SELECT incident_id,phase,updated_at FROM incident_agent_app.incidents WHERE incident_id=%s", (incident_id,))
        if not rows:
            raise RoundNotFound()
        return jsonable_encoder({**rows[0], "run": run_summary(self.latest(incident_id)),
                                 "model_called": False, "fresh_observation": False})

    def accept_interaction(self, incident_id, body, parent_id, references):
        payload = body.model_dump()
        digest = request_digest(payload)
        try:
            with self._connect() as conn:
                incident = conn.execute("SELECT * FROM incident_agent_app.incidents WHERE incident_id=%s FOR UPDATE", (incident_id,)).fetchone()
                if not incident:
                    raise RoundNotFound()
                replay = conn.execute("SELECT * FROM incident_agent_app.runs WHERE idempotency_scope=%s AND idempotency_key=%s",
                                      ("interaction:" + incident_id, body.client_message_id)).fetchone()
                if replay:
                    return self._replay(replay, digest)
                latest = conn.execute("SELECT * FROM incident_agent_app.runs WHERE incident_id=%s AND run_kind='diagnosis' ORDER BY created_at DESC,run_id DESC LIMIT 1", (incident_id,)).fetchone()
                if (latest["run_id"] if latest else None) != parent_id:
                    raise RoundConflict()
                historical = body.intent in {"explain", "compare"}
                if latest and latest["status"] not in TERMINAL and not (historical and latest["status"] in {"waiting_approval", "waiting_user"}):
                    raise RoundConflict()
                if conn.execute("SELECT 1 FROM incident_agent_app.runs WHERE incident_id=%s AND run_kind='interaction' AND status NOT IN ('succeeded','failed','cancelled')", (incident_id,)).fetchone():
                    raise RoundConflict()
                if not historical:
                    state = references[0]["state"]
                    manual = state.get("phase") == "remediation_planned" and not state.get("requires_approval")
                    if (state.get("phase") not in TERMINAL_PHASES and not manual) or state.get("approval_status") == "pending":
                        raise RoundConflict()
                    if not latest and state.get("approved") and state.get("phase") != "verification_succeeded":
                        raise RoundConflict()
                    if conn.execute("SELECT 1 FROM incident_agent_app.operations WHERE incident_id=%s AND state IN ('prepared','dispatching','outcome_unknown','manual_required')", (incident_id,)).fetchone():
                        raise RoundConflict()
                elif not latest and incident["phase"] not in TERMINAL_PHASES | {"awaiting_approval", "remediation_planned"}:
                    raise RoundConflict()
                # Historical questions are the only new user inputs accepted
                # during approval waiting. Invalidation/resume belongs to 4B-2.
                message = conn.execute("SELECT * FROM incident_agent_app.messages WHERE incident_id=%s AND role='user' AND client_message_id=%s",
                                       (incident_id, body.client_message_id)).fetchone()
                if message and (message["content"] != body.content or message["related_run_id"] is not None):
                    raise IdempotencyConflict()
                if not message:
                    message = conn.execute("""INSERT INTO incident_agent_app.messages
                        (message_id,incident_id,sequence,client_message_id,role,source,content)
                        SELECT %s,%s,COALESCE(MAX(sequence),0)+1,%s,'user','user_supplied',%s
                        FROM incident_agent_app.messages WHERE incident_id=%s RETURNING *""",
                        (str(uuid4()), incident_id, body.client_message_id, body.content, incident_id)).fetchone()
                context = {"message_id": message["message_id"], "references": references}
                row = conn.execute("""INSERT INTO incident_agent_app.runs
                    (run_id,incident_id,thread_id,run_kind,parent_run_id,input_payload,input_sha256,
                     workflow_version,idempotency_scope,idempotency_key,request_sha256,context_snapshot,context_sha256)
                    VALUES (%s,%s,%s,'interaction',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (str(uuid4()), incident_id, str(uuid4()), parent_id, Jsonb(payload), digest, INTERACTION_WORKFLOW,
                     "interaction:" + incident_id, body.client_message_id, digest, Jsonb(context), request_digest(context))).fetchone()
            return row
        except psycopg.Error as error:
            raise RunError() from error

    def progress(self, lease):
        return self.get_round(lease["incident_id"], lease["run_id"])["interaction_progress"]

    def save_progress(self, lease, progress):
        with self.fence(lease) as conn:
            conn.execute("UPDATE incident_agent_app.runs SET interaction_progress=%s WHERE run_id=%s", (Jsonb(progress), lease["run_id"]))

    def saved_recheck(self, lease):
        rows = self._read("SELECT result FROM incident_agent_app.rechecks WHERE run_id=%s", (lease["run_id"],))
        return rows[0]["result"] if rows else None

    def save_recheck(self, lease, result):
        with self.fence(lease) as conn:
            conn.execute("""INSERT INTO incident_agent_app.rechecks(recheck_id,incident_id,result,run_id)
                VALUES (%s,%s,%s,%s) ON CONFLICT (run_id) DO NOTHING""",
                (result.recheck_id, lease["incident_id"], Jsonb(result.model_dump(mode="json")), lease["run_id"]))

    def complete(self, lease, output, *, assistant=None, investigate=False):
        with self.fence(lease) as conn:
            conn.execute("SELECT 1 FROM incident_agent_app.incidents WHERE incident_id=%s FOR UPDATE", (lease["incident_id"],))
            # Release this lane inside the transaction, then accept the child.
            # The result and child ID commit together, including after recovery.
            conn.execute("""UPDATE incident_agent_app.runs SET status='succeeded',finished_at=clock_timestamp(),
                updated_at=clock_timestamp(),lease_owner=NULL,lease_expires_at=NULL,last_error=NULL WHERE run_id=%s""", (lease["run_id"],))
            if investigate:
                child = RoundRepository(lambda: nullcontext(conn)).accept_round(
                    lease["incident_id"], lease["context_snapshot"]["message_id"], "interaction-" + lease["run_id"],
                    lease["parent_run_id"], lease["context_snapshot"]["references"][0]["state"])
                output = {**output, "diagnosis_run_id": child["run_id"]}
            if assistant:
                PostgresMessageRepository(lambda: nullcontext(conn)).append(lease["incident_id"], MessageDraft(
                    client_message_id="reply:" + lease["run_id"], role="assistant", related_run_id=lease["run_id"],
                    content=assistant, evidence_refs=[c["citation_id"] for c in output.get("citations", [])]))
            conn.execute("UPDATE incident_agent_app.runs SET output_snapshot=%s WHERE run_id=%s", (Jsonb(output), lease["run_id"]))
