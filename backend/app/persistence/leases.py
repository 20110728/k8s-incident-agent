"""Short PostgreSQL transactions for ownership and fenced state publication."""
from contextlib import contextmanager

from psycopg.types.json import Jsonb

from backend.app.persistence.runs import PostgresRunRepository


class LeaseLost(RuntimeError):
    pass


class UnsafeRetry(RuntimeError):
    """An ordinary retry must never clear an external-operation obligation."""


class LeaseRepository(PostgresRunRepository):
    def announce(self, owner: str, seconds: float) -> None:
        with self._connect() as connection:
            connection.execute("""INSERT INTO incident_agent_app.workers (worker_id,expires_at)
                VALUES (%s,clock_timestamp()+%s*interval '1 second')
                ON CONFLICT (worker_id) DO UPDATE SET expires_at=EXCLUDED.expires_at""", (owner, seconds))

    def withdraw(self, owner: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM incident_agent_app.workers WHERE worker_id=%s", (owner,))

    def claim(self, owner: str, seconds: float, max_attempts: int = 3) -> dict | None:
        with self._connect() as connection:
            with connection.transaction():
                # One locked row only; no transaction survives into model/tool work.
                row = connection.execute("""SELECT * FROM incident_agent_app.runs
                    WHERE status='queued'
                       OR (status='retry_scheduled' AND next_retry_at<=clock_timestamp())
                       OR (status='running' AND lease_expires_at<=clock_timestamp())
                    ORDER BY created_at,run_id LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
                if row is None:
                    return None
                recovery_only = row["attempt"] - row.get("attempt_base", 0) >= max_attempts
                claimed = connection.execute("""UPDATE incident_agent_app.runs SET
                    status='running',lease_owner=%s,lease_epoch=lease_epoch+1,attempt=attempt+%s,
                    heartbeat_at=clock_timestamp(),lease_expires_at=clock_timestamp()+%s*interval '1 second',
                    updated_at=clock_timestamp(),next_retry_at=NULL
                    WHERE run_id=%s RETURNING *""", (owner, 0 if recovery_only else 1, seconds, row["run_id"])).fetchone()
                # Even at the budget limit we may project a saved END/interrupt;
                # this lease may never start or continue a graph.
                claimed["recovery_only"] = recovery_only
                return claimed

    @contextmanager
    def fence(self, lease: dict):
        with self._connect() as connection:
            with connection.transaction():
                # Bound a failed storage operation so it cannot pin a claim forever.
                connection.execute("SET LOCAL lock_timeout = '5s'")
                connection.execute("SELECT incident_id FROM incident_agent_app.incidents WHERE incident_id=%s FOR UPDATE", (lease["incident_id"],))
                row = connection.execute("""SELECT run_id FROM incident_agent_app.runs
                    WHERE run_id=%s AND lease_owner=%s AND lease_epoch=%s
                      AND status='running' AND lease_expires_at>clock_timestamp()
                    FOR UPDATE""", (lease["run_id"], lease["lease_owner"], lease["lease_epoch"])).fetchone()
                if row is None:
                    raise LeaseLost("run lease is no longer owned")
                if not connection.execute("SELECT lease_expires_at>clock_timestamp() AS valid FROM incident_agent_app.runs WHERE run_id=%s", (lease["run_id"],)).fetchone()["valid"]:
                    raise LeaseLost("run lease expired while waiting for its lock")
                yield connection

    def assert_owned(self, lease: dict) -> None:
        with self.fence(lease):
            pass

    def mark_checkpoint_started(self, lease: dict) -> None:
        # Commit BEFORE checkpoint IO. A crash in between is conservatively
        # classified as missing state rather than permission to restart START.
        with self.fence(lease) as connection:
            connection.execute("UPDATE incident_agent_app.runs SET checkpoint_started=TRUE WHERE run_id=%s",
                               (lease["run_id"],))

    def heartbeat(self, lease: dict, seconds: float) -> None:
        with self.fence(lease) as connection:
            connection.execute("""UPDATE incident_agent_app.runs SET heartbeat_at=clock_timestamp(),
                lease_expires_at=clock_timestamp()+%s*interval '1 second'
                WHERE run_id=%s""", (seconds, lease["run_id"]))

    def finish(self, lease: dict, status: str, *, phase: str | None = None,
               error_code: str | None = None, retry_seconds: float | None = None,
               output_snapshot: dict | None = None, question: dict | None = None,
               adopted_message_ids: list | None = None) -> str:
        if status not in {"succeeded", "failed", "waiting_approval", "waiting_user", "retry_scheduled", "reconciling", "cancelled"}:
            raise ValueError("invalid worker result status")
        if (status == "retry_scheduled") != (retry_seconds is not None):
            raise ValueError("retry delay is required only for retry_scheduled")
        with self.fence(lease) as connection:
            current = connection.execute("SELECT stop_requested FROM incident_agent_app.runs WHERE run_id=%s", (lease["run_id"],)).fetchone()
            if current["stop_requested"] and status in {"succeeded", "failed", "waiting_user", "waiting_approval"}:
                operation = connection.execute("SELECT state FROM incident_agent_app.operations WHERE run_id=%s", (lease["run_id"],)).fetchone()
                status = "reconciling" if operation and operation["state"] in {"dispatching", "outcome_unknown", "manual_required"} else "cancelled"
                phase = status
                error_code = "CONTROL_PENDING_RECONCILIATION" if status == "reconciling" else "INPUT_SUPERSEDED"
            if status == "waiting_user" and question:
                connection.execute("UPDATE incident_agent_app.runs SET question_payload=%s,answer_payload=NULL WHERE run_id=%s", (Jsonb(question), lease["run_id"]))
            elif status in {"succeeded", "failed", "cancelled"}:
                connection.execute("UPDATE incident_agent_app.runs SET question_payload=NULL WHERE run_id=%s", (lease["run_id"],))
            if adopted_message_ids is not None:
                connection.execute("UPDATE incident_agent_app.runs SET adopted_message_ids=%s WHERE run_id=%s", (Jsonb(adopted_message_ids), lease["run_id"]))
            if status == "retry_scheduled" and connection.execute("""SELECT operation_id
                    FROM incident_agent_app.operations WHERE run_id=%s
                    AND state NOT IN ('prepared','rejected')""", (lease["run_id"],)).fetchone():
                raise UnsafeRetry("external operation requires reconciliation")
            connection.execute("""UPDATE incident_agent_app.runs SET status=%s,
                last_error=%s,updated_at=clock_timestamp(),
                finished_at=CASE WHEN %s IN ('succeeded','failed','cancelled') THEN clock_timestamp() ELSE NULL END,
                next_retry_at=CASE WHEN %s::double precision IS NOT NULL THEN clock_timestamp()+%s*interval '1 second' ELSE NULL END,
                lease_owner=NULL,lease_expires_at=NULL WHERE run_id=%s""",
                (status, Jsonb({"code": error_code}) if error_code else None, status,
                 retry_seconds, retry_seconds, lease["run_id"]))
            if phase is not None:
                connection.execute("UPDATE incident_agent_app.incidents SET phase=%s,updated_at=clock_timestamp() WHERE incident_id=%s",
                                   (phase, lease["incident_id"]))
            if output_snapshot is not None and status in {"succeeded", "failed", "cancelled"}:
                connection.execute("UPDATE incident_agent_app.runs SET output_snapshot=COALESCE(output_snapshot,%s) WHERE run_id=%s",
                                   (Jsonb(output_snapshot), lease["run_id"]))
        return status
