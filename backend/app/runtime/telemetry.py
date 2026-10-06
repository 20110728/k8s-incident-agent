"""Structured correlation only: never emit prompts, credentials or exceptions."""
from datetime import datetime, UTC
from dataclasses import asdict, is_dataclass
import hashlib
import json
import os
import re


def digest(value):
    def encode(item):
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        if is_dataclass(item) and not isinstance(item, type):
            return asdict(item)
        return {"type": type(item).__name__}
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=encode)
        return hashlib.sha256(raw.encode()).hexdigest()
    except Exception:
        return None


def _label(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else None


def report(event, lease=None, *, operation_id=None, node=None, error_code=None,
           error_class=None, input_value=None, output_value=None, elapsed_ms=None):
    row = lease or {}
    payload = {"event": _label(event), "timestamp": datetime.now(UTC).isoformat(), "pid": os.getpid(),
               **{key: _label(row.get(key)) for key in ("incident_id", "run_id", "thread_id", "lease_owner")},
               "epoch": row.get("lease_epoch"), "attempt": row.get("attempt"),
               "operation_id": _label(operation_id), "node": _label(node),
               "error_code": _label(error_code), "error_class": _label(error_class),
               "input_sha256": digest(input_value) if input_value is not None else _label(row.get("input_sha256")),
               "output_sha256": digest(output_value) if output_value is not None else None,
               "elapsed_ms": elapsed_ms}
    try:
        print(json.dumps(payload), flush=True)
    except (OSError, ValueError):
        # A log sink failure must not change the result of a committed operation.
        pass
