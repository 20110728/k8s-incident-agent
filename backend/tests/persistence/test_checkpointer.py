from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from pydantic import SecretStr

from backend.app.persistence import checkpointer
from backend.app.persistence.checkpointer import (
    postgres_checkpointer,
)
from backend.app.persistence.settings import DatabaseSettings


class FakeSaver:
    def __init__(self) -> None:
        self.setup_count = 0

    def setup(self) -> None:
        self.setup_count += 1


def test_postgres_checkpointer_initializes_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver = FakeSaver()
    events: list[str] = []
    received_dsns: list[str] = []

    class FakePostgresSaver:
        @staticmethod
        @contextmanager
        def from_conn_string(
            conn_string: str,
            *,
            pipeline: bool = False,
        ) -> Iterator[FakeSaver]:
            received_dsns.append(conn_string)
            assert pipeline is False
            events.append("entered")
            try:
                yield saver
            finally:
                events.append("exited")

    monkeypatch.setattr(
        checkpointer,
        "_postgres_saver_class",
        lambda: FakePostgresSaver,
    )
    monkeypatch.delenv(
        "LANGGRAPH_STRICT_MSGPACK",
        raising=False,
    )
    settings = DatabaseSettings(
        database_url=SecretStr(
            "postgresql+psycopg://user:secret@db/app"
        ),
        _env_file=None,
    )

    with postgres_checkpointer(settings) as result:
        assert result is saver
        assert events == ["entered"]
        assert saver.setup_count == 1
        assert os.environ[
            "LANGGRAPH_STRICT_MSGPACK"
        ] == "true"

    assert events == ["entered", "exited"]
    assert received_dsns == [
        "postgresql://user:secret@db/app"
    ]