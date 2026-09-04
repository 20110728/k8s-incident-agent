from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from backend.app.persistence.database import normalize_psycopg_dsn
from backend.app.persistence.settings import DatabaseSettings


def _postgres_saver_class() -> type[Any]:
    from langgraph.checkpoint.postgres import PostgresSaver

    return PostgresSaver


@contextmanager
def postgres_checkpointer(
    settings: DatabaseSettings,
) -> Iterator[Any]:
    """Keep one initialized PostgreSQL saver open for its caller."""

    os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
    dsn = normalize_psycopg_dsn(
        settings.database_url.get_secret_value()
    )
    saver_class = _postgres_saver_class()

    with saver_class.from_conn_string(dsn) as saver:
        saver.setup()
        yield saver