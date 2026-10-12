"""One durable observation per automatic verification/explicit interaction."""
from contextlib import contextmanager
from uuid import uuid4
from psycopg.types.json import Jsonb

from backend.app.persistence.leases import LeaseLost


class ObservationStore:
    def __init__(self, connect, key, incident_id, *, repo=None, lease=None):
        self.connect, self.key, self.incident_id = connect, key, incident_id
        self.repo, self.lease, self.token = repo, lease, str(uuid4())

    @contextmanager
    def transaction(self):
        if self.lease is not None:
            with self.repo.fence(self.lease) as conn:
                yield conn
        else:
            with self.connect() as conn:
                conn.execute("SELECT incident_id FROM incident_agent_app.incidents WHERE incident_id=%s FOR UPDATE", (self.incident_id,))
                yield conn

    def open(self, initial):
        with self.transaction() as conn:
            conn.execute("""INSERT INTO incident_agent_app.observation_windows
                (observation_key,incident_id,run_id,owner_token,payload) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (observation_key) DO NOTHING""",
                (self.key, self.incident_id, self.lease["run_id"] if self.lease else None, self.token, Jsonb(initial)))
            row = conn.execute("""UPDATE incident_agent_app.observation_windows SET owner_token=%s
                WHERE observation_key=%s RETURNING payload""", (self.token, self.key)).fetchone()
            return row["payload"]

    def save(self, payload):
        with self.transaction() as conn:
            row = conn.execute("""UPDATE incident_agent_app.observation_windows
                SET payload=%s,updated_at=clock_timestamp() WHERE observation_key=%s AND owner_token=%s
                RETURNING observation_key""", (Jsonb(payload), self.key, self.token)).fetchone()
            if row is None:
                raise LeaseLost("observation ownership changed")
            if payload.get("status") == "passed":
                from backend.app.persistence.delayed_rechecks import schedule
                schedule(conn, self.key, self.incident_id, self.lease, payload)
