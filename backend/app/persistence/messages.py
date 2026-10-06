"""Append-only messages. Parent row lock serializes sequence allocation/commit."""
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from backend.app.services.message_schemas import MessageDraft, MessageReceipt, MessagePage


class MessageError(ValueError):
    def __init__(self, code, status):
        self.code, self.status = code, status
        super().__init__(code)


class MessageStorageError(RuntimeError):
    pass


class PostgresMessageRepository:
    def __init__(self, connection_factory):
        self.connection_factory = connection_factory

    def append(self, incident_id: str, draft: MessageDraft) -> MessageReceipt:
        try:
            with self.connection_factory() as conn:
                incident = conn.execute("""SELECT phase FROM incident_agent_app.incidents
                    WHERE incident_id=%s FOR UPDATE""", (incident_id,)).fetchone()
                if incident is None:
                    raise MessageError("INCIDENT_NOT_FOUND", 404)
                existing = conn.execute("""SELECT * FROM incident_agent_app.messages
                    WHERE incident_id=%s AND role=%s AND client_message_id=%s""",
                    (incident_id, draft.role, draft.client_message_id)).fetchone()
                if existing:
                    if any(existing[key] != getattr(draft, key)
                           for key in ("content", "related_run_id", "evidence_refs")):
                        raise MessageError("MESSAGE_ID_CONFLICT", 409)
                    return MessageReceipt(message=existing, created=False)
                # Until 4B adds invalidation/resume, do not accept new user input
                # into a live workflow or an unresolved write. Retries above work.
                if draft.role == "user":
                    active = conn.execute("""SELECT 1 FROM incident_agent_app.runs
                        WHERE incident_id=%s AND status NOT IN ('succeeded','failed','cancelled')
                        UNION ALL SELECT 1 FROM incident_agent_app.operations
                        WHERE incident_id=%s AND state IN
                        ('prepared','dispatching','outcome_unknown','manual_required') LIMIT 1""",
                        (incident_id, incident_id)).fetchone()
                    if active or incident["phase"] in (
                        "awaiting_approval", "approved", "approval_approved", "executing_remediation"
                    ):
                        raise MessageError("MESSAGE_WORKFLOW_BUSY", 409)
                if draft.related_run_id is not None:
                    run = conn.execute("""SELECT 1 FROM incident_agent_app.runs
                        WHERE run_id=%s AND incident_id=%s""",
                        (draft.related_run_id, incident_id)).fetchone()
                    if run is None:
                        raise MessageError("MESSAGE_RUN_MISMATCH", 409)
                source = {"user": "user_supplied", "assistant": "model_generated", "tool": "tool_observed"}[draft.role]
                row = conn.execute("""INSERT INTO incident_agent_app.messages
                    (message_id,incident_id,sequence,client_message_id,role,source,content,related_run_id,evidence_refs)
                    SELECT %s,%s,COALESCE(MAX(sequence),0)+1,%s,%s,%s,%s,%s,%s
                    FROM incident_agent_app.messages WHERE incident_id=%s RETURNING *""",
                    (str(uuid4()), incident_id, draft.client_message_id, draft.role, source,
                     draft.content, draft.related_run_id, Jsonb(draft.evidence_refs), incident_id)).fetchone()
                receipt = MessageReceipt(message=row, created=True)
            return receipt  # Do not acknowledge a write until commit succeeds.
        except psycopg.Error as error:
            raise MessageStorageError("Could not persist message") from error

    def list(self, incident_id, limit=20, before_sequence=None) -> MessagePage:
        if not 1 <= limit <= 50 or (before_sequence is not None and before_sequence < 1):
            raise ValueError("Invalid message pagination")
        try:
            with self.connection_factory() as conn:
                if conn.execute("SELECT 1 FROM incident_agent_app.incidents WHERE incident_id=%s",
                                (incident_id,)).fetchone() is None:
                    raise MessageError("INCIDENT_NOT_FOUND", 404)
                rows = conn.execute("""SELECT * FROM incident_agent_app.messages
                    WHERE incident_id=%s AND (%s::bigint IS NULL OR sequence<%s)
                    ORDER BY sequence DESC LIMIT %s""",
                    (incident_id, before_sequence, before_sequence, limit + 1)).fetchall()
            return MessagePage(items=rows[:limit], next_before_sequence=(
                rows[limit - 1]["sequence"] if len(rows) > limit else None))
        except psycopg.Error as error:
            raise MessageStorageError("Could not read messages") from error
