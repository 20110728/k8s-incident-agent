from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any


class FakeResult:
    def __init__(
        self,
        row: Mapping[str, Any] | None,
    ) -> None:
        self._row = row

    def fetchone(self) -> Mapping[str, Any] | None:
        return self._row


class FakeConnection(AbstractContextManager):
    def __init__(self) -> None:
        self.results: list[
            Mapping[str, Any] | None
        ] = []
        self.calls: list[dict[str, Any]] = []
        self.enter_count = 0
        self.exit_count = 0
        self.execute_error: Exception | None = None

    def queue_result(
        self,
        row: Mapping[str, Any] | None,
    ) -> None:
        self.results.append(row)

    def execute(
        self,
        query: str,
        params: tuple[Any, ...] | None = None,
    ) -> FakeResult:
        if self.execute_error is not None:
            raise self.execute_error

        self.calls.append(
            {
                "query": " ".join(query.split()),
                "params": params,
            }
        )

        row = self.results.pop(0)
        return FakeResult(row)

    def __enter__(self) -> "FakeConnection":
        self.enter_count += 1
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.exit_count += 1


class FakeConnectionFactory:
    def __init__(
        self,
        connection: FakeConnection,
    ) -> None:
        self.connection = connection
        self.call_count = 0

    def __call__(self) -> FakeConnection:
        self.call_count += 1
        return self.connection


class FakeMigrationCursor(AbstractContextManager):
    def __init__(
        self,
        applied_versions: list[int],
    ) -> None:
        self.applied_versions = applied_versions
        self.calls: list[dict[str, Any]] = []

    def execute(
        self,
        query: str,
        params: tuple[Any, ...] | None = None,
    ) -> None:
        self.calls.append(
            {
                "query": " ".join(query.split()),
                "params": params,
            }
        )

    def fetchall(self) -> list[dict[str, int]]:
        return [
            {"version": version}
            for version in self.applied_versions
        ]

    def __enter__(self) -> "FakeMigrationCursor":
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        return None


class FakeTransaction(AbstractContextManager):
    def __init__(self) -> None:
        self.enter_count = 0
        self.exit_count = 0

    def __enter__(self) -> "FakeTransaction":
        self.enter_count += 1
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.exit_count += 1


class FakeMigrationConnection:
    def __init__(
        self,
        applied_versions: list[int] | None = None,
    ) -> None:
        self.cursor_value = FakeMigrationCursor(
            applied_versions or []
        )
        self.transaction_value = FakeTransaction()

    def cursor(self) -> FakeMigrationCursor:
        return self.cursor_value

    def transaction(self) -> FakeTransaction:
        return self.transaction_value