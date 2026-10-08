"""Event-row arbitration for human input, cancellation and write dispatch."""
from contextlib import nullcontext
from functools import wraps
import json
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from backend.app.persistence.interactions import InteractionRepository, TERMINAL
from backend.app.persistence.rounds import RoundRepository, RoundConflict, RoundNotFound
from backend.app.persistence.runs import IdempotencyConflict, request_digest, RunError


def storage_errors(method):
    @wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except psycopg.Error as error:
            raise RunError() from error
    return guarded


def message_in_transaction(conn, incident_id, key, content):
    row = conn.execute("SELECT * FROM incident_agent_app.messages WHERE incident_id=%s AND role='user' AND client_message_id=%s",
                       (incident_id, key)).fetchone()
    if row:
        if row["content"] != content:
            raise IdempotencyConflict()
        return row
    return conn.execute("""INSERT INTO incident_agent_app.messages(message_id,incident_id,sequence,client_message_id,role,source,content)
        SELECT %s,%s,COALESCE(MAX(sequence),0)+1,%s,'user','user_supplied',%s
        FROM incident_agent_app.messages WHERE incident_id=%s RETURNING *""",
        (str(uuid4()), incident_id, key, content, incident_id)).fetchone()


def view(row):
    return {"control_id": row["control_id"], "status": row["status"], "message_id": row["message_id"], **row["result"]}


class ControlRepository(InteractionRepository):
    def activate_delayed(self):
        from backend.app.persistence.delayed_rechecks import activate_due
        activate_due(self)

    def find_control(self, incident_id, key):
        rows = self._read("SELECT * FROM incident_agent_app.controls WHERE incident_id=%s AND client_message_id=%s", (incident_id, key))
        if not rows:
            raise RoundNotFound()
        return view(rows[0])

    @storage_errors
    def control(self, incident_id, body, expected_parent, snapshot):
        payload = body.model_dump()
        digest = request_digest(payload)
        with self._connect() as conn:
            conn.execute("SET LOCAL lock_timeout = '5s'")
            incident = conn.execute("SELECT * FROM incident_agent_app.incidents WHERE incident_id=%s FOR UPDATE", (incident_id,)).fetchone()
            if not incident:
                raise RoundNotFound()
            replay = conn.execute("SELECT * FROM incident_agent_app.controls WHERE incident_id=%s AND client_message_id=%s", (incident_id, body.client_message_id)).fetchone()
            if replay:
                if replay["request_sha256"] != digest:
                    raise IdempotencyConflict()
                return view(replay)
            latest = conn.execute("SELECT * FROM incident_agent_app.runs WHERE incident_id=%s AND run_kind='diagnosis' ORDER BY created_at DESC,run_id DESC LIMIT 1 FOR UPDATE", (incident_id,)).fetchone()
            if (latest["run_id"] if latest else None) != expected_parent:
                raise RoundConflict()
            if latest is None:
                raise RoundConflict()  # Legacy no-run checkpoints retain their original read-only compatibility path.
            message = message_in_transaction(conn, incident_id, body.client_message_id, body.content)
            if message["sequence"] <= (latest["input_message_sequence"] or 0) or conn.execute(
                "SELECT 1 FROM incident_agent_app.runs WHERE source_message_id=%s", (message["message_id"],)
            ).fetchone():
                raise IdempotencyConflict()
            operation = conn.execute("SELECT * FROM incident_agent_app.operations WHERE run_id=%s", (latest["run_id"],)).fetchone()
            dispatched = bool(operation and operation["state"] not in {"prepared", "rejected"})
            conn.execute("UPDATE incident_agent_app.incidents SET event_revision=event_revision+1 WHERE incident_id=%s", (incident_id,))
            active = latest["status"] not in TERMINAL
            if active:
                conn.execute("UPDATE incident_agent_app.runs SET stop_requested=TRUE,invalidated_at=clock_timestamp() WHERE run_id=%s", (latest["run_id"],))
                if not dispatched:
                    conn.execute("""UPDATE incident_agent_app.operations SET state='rejected',error_code='INPUT_SUPERSEDED',
                        updated_at=clock_timestamp() WHERE run_id=%s AND state='prepared'""", (latest["run_id"],))
                    conn.execute("""UPDATE incident_agent_app.runs SET status='cancelled',finished_at=clock_timestamp(),
                        updated_at=clock_timestamp(),lease_owner=NULL,lease_expires_at=NULL,question_payload=NULL,
                        output_snapshot=COALESCE(output_snapshot,%s),last_error='{"code":"INPUT_SUPERSEDED"}'::jsonb
                        WHERE run_id=%s""", (Jsonb(snapshot), latest["run_id"]))
                    conn.execute("UPDATE incident_agent_app.incidents SET phase='cancelled',updated_at=clock_timestamp() WHERE incident_id=%s", (incident_id,))
            # A stopped/superseded interaction must not launch a child diagnosis later.
            conn.execute("""UPDATE incident_agent_app.runs SET status='cancelled',stop_requested=TRUE,
                invalidated_at=clock_timestamp(),finished_at=clock_timestamp(),lease_owner=NULL,lease_expires_at=NULL,
                last_error='{"code":"INPUT_SUPERSEDED"}'::jsonb
                WHERE incident_id=%s AND run_kind='interaction' AND status NOT IN ('succeeded','failed','cancelled')""", (incident_id,))
            if body.action in {"stop", "investigate"}:
                conn.execute("UPDATE incident_agent_app.controls SET status='superseded' WHERE incident_id=%s AND status='pending'", (incident_id,))
            status = "pending" if body.action == "investigate" else "saved"
            result = {"action": body.action, "parent_run_id": latest["run_id"],
                      "deferred_until_write_checked": bool(dispatched and (active or operation["state"] in {"dispatching", "outcome_unknown", "manual_required"})), "note_source": "user_supplied_unverified",
                      "model_called": False, "cluster_writes_executed": False}
            row = conn.execute("""INSERT INTO incident_agent_app.controls
                (control_id,incident_id,client_message_id,request_sha256,request,message_id,parent_run_id,snapshot,status,result)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (str(uuid4()), incident_id, body.client_message_id, digest, Jsonb(payload), message["message_id"],
                 latest["run_id"], Jsonb(snapshot), status, Jsonb(result))).fetchone()
        return view(row)

    @storage_errors
    def activate_pending(self):
        candidates = self._read("""SELECT c.control_id,c.incident_id FROM incident_agent_app.controls c
            JOIN incident_agent_app.runs r ON r.run_id=c.parent_run_id WHERE c.status='pending'
            AND r.status IN ('succeeded','failed','cancelled')
            AND NOT EXISTS(SELECT 1 FROM incident_agent_app.operations o WHERE o.incident_id=c.incident_id
                AND o.state IN ('prepared','dispatching','outcome_unknown','manual_required'))
            ORDER BY c.created_at LIMIT 20""", ())
        for candidate in candidates:
            with self._connect() as conn:
                conn.execute("SET LOCAL lock_timeout = '5s'")
                conn.execute("SELECT 1 FROM incident_agent_app.incidents WHERE incident_id=%s FOR UPDATE", (candidate["incident_id"],))
                control = conn.execute("SELECT * FROM incident_agent_app.controls WHERE control_id=%s FOR UPDATE", (candidate["control_id"],)).fetchone()
                if control["status"] != "pending":
                    continue
                latest = conn.execute("SELECT * FROM incident_agent_app.runs WHERE incident_id=%s AND run_kind='diagnosis' ORDER BY created_at DESC,run_id DESC LIMIT 1", (control["incident_id"],)).fetchone()
                if latest["run_id"] != control["parent_run_id"]:
                    conn.execute("UPDATE incident_agent_app.controls SET status='superseded' WHERE control_id=%s", (control["control_id"],))
                    continue
                if latest["status"] not in TERMINAL:
                    continue
                if conn.execute("""SELECT 1 FROM incident_agent_app.operations WHERE incident_id=%s
                    AND state IN ('prepared','dispatching','outcome_unknown','manual_required')""", (control["incident_id"],)).fetchone():
                    continue
                if conn.execute("SELECT 1 FROM incident_agent_app.runs WHERE incident_id=%s AND run_kind='interaction' AND status NOT IN ('succeeded','failed','cancelled')", (control["incident_id"],)).fetchone():
                    continue
                # Include later saved facts; stopping explicitly supersedes the pending request.
                message = conn.execute("SELECT message_id FROM incident_agent_app.messages WHERE incident_id=%s AND role='user' ORDER BY sequence DESC LIMIT 1", (control["incident_id"],)).fetchone()
                previous = latest["output_snapshot"] or control["snapshot"]
                child = RoundRepository(lambda: nullcontext(conn)).accept_round(control["incident_id"], message["message_id"],
                    "control-" + control["control_id"], latest["run_id"], previous)
                result = {**control["result"], "diagnosis_run_id": child["run_id"], "deferred_until_write_checked": False}
                conn.execute("UPDATE incident_agent_app.controls SET status='started',result=%s WHERE control_id=%s", (Jsonb(result), control["control_id"]))

    @storage_errors
    def answer(self, incident_id, run_id, body):
        payload = {"action": "answer", "run_id": run_id, **body.model_dump()}
        digest = request_digest(payload)
        with self._connect() as conn:
            conn.execute("SET LOCAL lock_timeout = '5s'")
            if not conn.execute("SELECT 1 FROM incident_agent_app.incidents WHERE incident_id=%s FOR UPDATE", (incident_id,)).fetchone():
                raise RoundNotFound()
            replay = conn.execute("SELECT * FROM incident_agent_app.controls WHERE incident_id=%s AND client_message_id=%s", (incident_id, body.client_message_id)).fetchone()
            if replay:
                if replay["request_sha256"] != digest:
                    raise IdempotencyConflict()
                return view(replay)
            row = conn.execute("SELECT * FROM incident_agent_app.runs WHERE incident_id=%s AND run_id=%s FOR UPDATE", (incident_id, run_id)).fetchone()
            if not row:
                raise RoundNotFound()
            question = row["question_payload"] or {}
            if (row["status"] != "waiting_user" or row["stop_requested"] or question.get("question_id") != body.question_id
                or question.get("version") != body.version):
                raise RoundConflict()
            if not body.skip and set(body.answers) != {q["slot"] for q in question["questions"]}:
                raise RoundConflict()
            content = body.content + "\n" + json.dumps({"answers": body.answers, "skip": body.skip}, ensure_ascii=False)
            message = message_in_transaction(conn, incident_id, body.client_message_id, content)
            revision = conn.execute("UPDATE incident_agent_app.incidents SET event_revision=event_revision+1 WHERE incident_id=%s RETURNING event_revision", (incident_id,)).fetchone()["event_revision"]
            answer = {"question_id": body.question_id, "version": body.version, "answers": body.answers,
                      "skip": body.skip, "message_id": message["message_id"], "sequence": message["sequence"],
                      "evidence_revision": question.get("evidence_revision"), "accepted_at": message["created_at"].isoformat(),
                      "questions": question["questions"], "question_source": "policy_generated",
                      "source": "user_supplied_unverified"}
            conn.execute("""UPDATE incident_agent_app.runs SET answer_payload=%s,status='queued',event_revision=%s,
                attempt_base=attempt,updated_at=clock_timestamp() WHERE run_id=%s""", (Jsonb(answer), revision, run_id))
            result = {"run_id": run_id, "thread_id": row["thread_id"], "question_id": body.question_id,
                      "version": body.version, "processing": "queued", "note_source": "user_supplied_unverified"}
            receipt = conn.execute("""INSERT INTO incident_agent_app.controls
                (control_id,incident_id,client_message_id,request_sha256,request,message_id,parent_run_id,snapshot,status,result)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'{}','saved',%s) RETURNING *""", (str(uuid4()), incident_id,
                body.client_message_id, digest, Jsonb(payload), message["message_id"], run_id, Jsonb(result))).fetchone()
        return view(receipt)
