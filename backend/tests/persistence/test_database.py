from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from backend.app.persistence import database
from backend.app.persistence.database import (
    connect_database,
    normalize_psycopg_dsn,
)
from backend.app.persistence.settings import DatabaseSettings


def build_settings(
    database_url: str = (
        "postgresql+psycopg://user:secret@db:5432/app"
    ),
    *,
    timeout: int = 5,
) -> DatabaseSettings:
    return DatabaseSettings(
        database_url=SecretStr(database_url),
        connect_timeout_seconds=timeout,
        _env_file=None,
    )


@pytest.mark.parametrize(
    ("database_url", "expected"),
    [
        (
            "postgresql+psycopg://user:pw@db/app",
            "postgresql://user:pw@db/app",
        ),
        (
            "postgres+psycopg://user:pw@db/app",
            "postgres://user:pw@db/app",
        ),
        (
            "postgresql://user:pw@db/app",
            "postgresql://user:pw@db/app",
        ),
        (
            "postgres://user:pw@db/app",
            "postgres://user:pw@db/app",
        ),
    ],
)
def test_normalize_psycopg_dsn(
    database_url: str,
    expected: str,
) -> None:
    assert normalize_psycopg_dsn(database_url) == expected


@pytest.mark.parametrize(
    "database_url",
    [
        "",
        "   ",
        "mysql://user:pw@db/app",
        "sqlite:///tmp/app.db",
    ],
)
def test_normalize_psycopg_dsn_rejects_invalid_url(
    database_url: str,
) -> None:
    with pytest.raises(ValueError):
        normalize_psycopg_dsn(database_url)


def test_database_settings_reads_existing_pgvector_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PGVECTOR_URL",
        "postgresql+psycopg://user:secret@db/app",
    )

    settings = DatabaseSettings(_env_file=None)

    assert settings.database_url.get_secret_value() == (
        "postgresql+psycopg://user:secret@db/app"
    )
    assert "secret" not in repr(settings)


def test_database_settings_requires_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PGVECTOR_URL", raising=False)

    with pytest.raises(ValidationError):
        DatabaseSettings(_env_file=None)


def test_connect_database_uses_expected_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    expected_connection = object()

    def fake_connect(
        dsn: str,
        **kwargs: Any,
    ) -> object:
        calls.append(
            {
                "dsn": dsn,
                **kwargs,
            }
        )
        return expected_connection

    monkeypatch.setattr(
        database.psycopg,
        "connect",
        fake_connect,
    )

    connection = connect_database(
        build_settings(timeout=7),
        autocommit=True,
    )

    assert connection is expected_connection
    assert calls == [
        {
            "dsn": (
                "postgresql://user:secret@db:5432/app"
            ),
            "autocommit": True,
            "connect_timeout": 7,
            "row_factory": database.dict_row,
        }
    ]