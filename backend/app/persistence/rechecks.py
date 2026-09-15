"""Append-only recheck results, separate from workflow checkpoints and approvals."""

import psycopg
from psycopg.types.json import Jsonb


class RecheckRepositoryError(RuntimeError):
    pass


class PostgresRecheckRepository:
    def __init__(self, connection_factory):
        self.connection_factory = connection_factory

    def append(self, result):
        # connect_database returns a transactional Psycopg connection: leaving
        # this context commits; an exception rolls back. Never update old rows.
        try:
            with self.connection_factory() as conn:
                conn.execute(
                    """INSERT INTO incident_agent_app.rechecks
                    (recheck_id, incident_id, result) VALUES (%s, %s, %s)""",
                    (
                        result.recheck_id,
                        result.incident_id,
                        Jsonb(result.model_dump(mode="json")),
                    ),
                )
        except psycopg.Error as error:
            raise RecheckRepositoryError("Could not persist recheck.") from error

    def list(self, incident_id, limit=20, before_sequence=None):
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        try:
            # sequence is insertion order, not collection time. A cursor avoids
            # offset shifts as new independent rechecks are appended.
            with self.connection_factory() as conn:
                rows = conn.execute(
                    """SELECT sequence, result FROM incident_agent_app.rechecks
                    WHERE incident_id = %s AND (%s::bigint IS NULL OR sequence < %s)
                    ORDER BY sequence DESC LIMIT %s""",
                    (incident_id, before_sequence, before_sequence, limit),
                ).fetchall()
        except psycopg.Error as error:
            raise RecheckRepositoryError("Could not read recheck history.") from error
        return {
            "items": [row["result"] for row in rows],
            "next_before_sequence": (
                rows[-1]["sequence"] if len(rows) == limit else None
            ),
        }
