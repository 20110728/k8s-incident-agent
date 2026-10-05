"""Fence BOTH full checkpoints and pending writes with the same run row lock."""
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver

from backend.app.persistence.database import normalize_psycopg_dsn
from backend.app.persistence.leases import LeaseLost


class FencedPostgresSaver(PostgresSaver):
    def bind(self, repository, lease, lost):
        self.repository, self.lease, self.lost = repository, lease, lost

    @contextmanager
    def _owned(self, config):
        if self.lost.is_set() or config["configurable"]["thread_id"] != self.lease["thread_id"]:
            raise LeaseLost("checkpoint ownership was lost")
        # Keep the row locked through the saver commit: a new claimant cannot
        # slip between the ownership check and the checkpoint/pending write.
        with self.repository.fence(self.lease):
            yield

    def put(self, config, checkpoint, metadata, new_versions):
        with self._owned(config):
            return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        with self._owned(config):
            return super().put_writes(config, writes, task_id, task_path)


@contextmanager
def fenced_checkpointer(settings, repository, lease, lost):
    dsn = normalize_psycopg_dsn(settings.database_url.get_secret_value())
    with FencedPostgresSaver.from_conn_string(dsn) as saver:
        saver.conn.execute("SET statement_timeout = '5s'")
        saver.setup()
        saver.bind(repository, lease, lost)
        yield saver
