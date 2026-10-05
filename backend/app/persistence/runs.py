"""Durable acceptance and metadata queries; no workflow execution lives here."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from psycopg import Error, errors
from psycopg.types.json import Jsonb

SCOPE = "single-operator:create-incident:v1"
KEY_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"


class RunError(Exception):
    status_code = 503
    code = "RUN_STORAGE_UNAVAILABLE"
    message = "Durable task storage is unavailable. Retry with the same key."


class IdempotencyConflict(RunError):
    status_code = 409
    code = "IDEMPOTENCY_CONFLICT"
    message = "This idempotency key belongs to a different request."


class InvalidRunQuery(RunError):
    status_code = 422
    code = "REQUEST_VALIDATION_ERROR"
    message = "The key, limit, or cursor is invalid for this query."


class QueuedModeRequired(RunError):
    status_code = 409
    code = "QUEUED_MODE_REQUIRED"
    message = "Idempotency keys require queued execution mode."


class QueuedExecutionUnavailable(RunError):
    status_code = 409
    code = "QUEUED_EXECUTION_UNAVAILABLE"
    message = "Queued workflow execution is not available in this release."


class RunNotFound(RunError):
    status_code = 404
    code = "INCIDENT_NOT_FOUND"
    message = "The incident or idempotency key was not found."


def validate_key(key: str | None) -> None:
    if key is not None and re.fullmatch(KEY_PATTERN, key) is None:
        raise InvalidRunQuery()


def request_digest(payload: dict) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def run_summary(row: dict | None) -> dict | None:
    if row is None:
        return None
    result = {key: row[key] for key in (
        "run_id", "status", "run_kind", "created_at", "updated_at", "finished_at", "attempt",
    )}
    # Never forward database exceptions, stack traces, or arbitrary error text.
    error = row.get("last_error")
    code = error.get("code") if isinstance(error, dict) else None
    result["last_error_code"] = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else None
    return result


def encode_cursor(scope: str, row: dict, id_field: str) -> str:
    value = {"v": 1, "scope": scope, "created_at": row["created_at"].isoformat(), "id": row[id_field]}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None, scope: str) -> tuple | None:
    if cursor is None:
        return None
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", cursor):
            raise ValueError()
        value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        if not isinstance(value, dict) or set(value) != {"v", "scope", "created_at", "id"}:
            raise ValueError()
        if type(value["v"]) is not int or value["v"] != 1 or value["scope"] != scope:
            raise ValueError()
        timestamp = datetime.fromisoformat(value["created_at"])
        if timestamp.tzinfo is None or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", value["id"]):
            raise ValueError()
        return timestamp, value["id"]
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise InvalidRunQuery() from error


class PostgresRunRepository:
    def __init__(self, connection_factory):
        self._connect = connection_factory

    def _read(self, sql: str, params: tuple = ()) -> list[dict]:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                    return cursor.fetchall()
        except Error as error:
            raise RunError() from error

    def by_key(self, key: str) -> dict | None:
        validate_key(key)
        rows = self._read("SELECT * FROM incident_agent_app.runs WHERE idempotency_scope=%s AND idempotency_key=%s", (SCOPE, key))
        return rows[0] if rows else None

    def latest(self, incident_id: str) -> dict | None:
        rows = self._read("SELECT * FROM incident_agent_app.runs WHERE incident_id=%s ORDER BY created_at DESC, run_id DESC LIMIT 1", (incident_id,))
        return rows[0] if rows else None

    def worker_available(self) -> bool:
        return self._read("SELECT EXISTS (SELECT 1 FROM incident_agent_app.workers WHERE expires_at>clock_timestamp()) AS available")[0]["available"]

    def accept(self, *, incident_id: str, run_id: str, thread_id: str, payload: dict, key: str | None) -> dict:
        validate_key(key)
        digest = request_digest(payload)
        if key is not None:
            existing = self.by_key(key)
            if existing is not None:
                return self._replay(existing, digest)
        try:
            with self._connect() as connection:
                with connection.transaction():
                    with connection.cursor() as cursor:
                        cursor.execute("""INSERT INTO incident_agent_app.incidents
                            (incident_id, thread_id, namespace, service_name, description)
                            VALUES (%s,%s,%s,%s,%s)""", (incident_id, thread_id, payload["namespace"], payload["service_name"], payload["description"]))
                        cursor.execute("""INSERT INTO incident_agent_app.runs
                            (run_id,incident_id,thread_id,input_payload,input_sha256,
                             idempotency_scope,idempotency_key,request_sha256)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                            (run_id, incident_id, thread_id, Jsonb(payload), digest, SCOPE if key is not None else None, key, digest))
                        row = cursor.fetchone()
            # Both transaction and connection contexts have committed before returning.
            return row
        except errors.UniqueViolation as error:
            if key is not None and error.diag.constraint_name == "runs_idempotency_unique":
                winner = self.by_key(key)
                if winner is not None:
                    return self._replay(winner, digest)
            raise RunError() from error
        except Error as error:
            raise RunError() from error

    @staticmethod
    def _replay(row: dict, digest: str) -> dict:
        if row["request_sha256"] != digest:
            raise IdempotencyConflict()
        return row

    def list_metadata(self, *, incident_id: str | None = None, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise InvalidRunQuery()
        scope = "incidents" if incident_id is None else "runs:" + incident_id
        position = decode_cursor(cursor, scope)
        conditions, params = [], []
        if incident_id is None:
            table, id_field = "incidents", "incident_id"
            fields = "t.incident_id,t.namespace,t.service_name,t.phase,t.created_at,t.updated_at"
        else:
            table, id_field = "runs", "run_id"
            fields = "t.run_id,t.status,t.run_kind,t.created_at,t.updated_at,t.finished_at,t.attempt,t.last_error"
            if not self._read("SELECT incident_id FROM incident_agent_app.incidents WHERE incident_id=%s", (incident_id,)):
                raise RunNotFound()
            conditions.append("t.incident_id=%s")
            params.append(incident_id)
        if position:
            conditions.append(f"(t.created_at,t.{id_field}) < (%s,%s)")
            params.extend(position)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self._read(f"SELECT {fields} FROM incident_agent_app.{table} t{where} ORDER BY t.created_at DESC,t.{id_field} DESC LIMIT %s", (*params, limit + 1))
        page = rows[:limit]
        next_cursor = encode_cursor(scope, page[-1], id_field) if len(rows) > limit else None
        if incident_id is None:
            for row in page:
                summaries = self._read("""SELECT run_id,status,run_kind,created_at,
                    updated_at,finished_at,attempt,last_error FROM incident_agent_app.runs
                    WHERE incident_id=%s ORDER BY created_at DESC,run_id DESC LIMIT 1""", (row["incident_id"],))
                row["run"] = run_summary(summaries[0] if summaries else None)
        else:
            page = [run_summary(row) for row in page]
        return {"items": page, "next_cursor": next_cursor}
