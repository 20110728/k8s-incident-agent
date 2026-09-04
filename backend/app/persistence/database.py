from __future__ import annotations

from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from backend.app.persistence.settings import DatabaseSettings


_PSYCOPG_SCHEMES = (
    "postgresql://",
    "postgres://",
)
_SQLALCHEMY_PSYCOPG_SCHEMES = {
    "postgresql+psycopg://": "postgresql://",
    "postgres+psycopg://": "postgres://",
}


def normalize_psycopg_dsn(database_url: str) -> str:
    """Convert the existing SQLAlchemy URL into a Psycopg DSN."""

    normalized = database_url.strip()

    if not normalized:
        raise ValueError("database URL must not be blank")

    for source, target in (
        _SQLALCHEMY_PSYCOPG_SCHEMES.items()
    ):
        if normalized.startswith(source):
            return target + normalized[len(source):]

    if normalized.startswith(_PSYCOPG_SCHEMES):
        return normalized

    raise ValueError(
        "database URL must use postgres, postgresql, "
        "postgres+psycopg, or postgresql+psycopg"
    )


def connect_database(
    settings: DatabaseSettings,
    *,
    autocommit: bool = False,
) -> Connection[Any]:
    """Open a Psycopg connection without logging its secret DSN."""

    dsn = normalize_psycopg_dsn(
        settings.database_url.get_secret_value()
    )

    return psycopg.connect(
        dsn,
        autocommit=autocommit,
        connect_timeout=(
            settings.connect_timeout_seconds
        ),
        row_factory=dict_row,
    )