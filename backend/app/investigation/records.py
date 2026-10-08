"""Replay and one shared correction slot, fenced by the existing run lease."""
from copy import deepcopy
from hashlib import sha256
import json


class IncompleteRequest(RuntimeError):
    pass


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")).encode()).hexdigest()


def saved_result(budget, ticket):
    with budget.edit() as data:
        call = data["calls"][ticket]
        if "result" not in call:
            raise IncompleteRequest("REQUEST_OUTCOME_UNKNOWN")
        return deepcopy(call["result"])


def bind_baseline(budget, baseline):
    value = digest(baseline)
    with budget.edit() as data:
        saved = data.setdefault("investigation", {"version": "readonly-investigation-v1", "baseline_digest": value})
        if saved["baseline_digest"] != value or saved["version"] != "readonly-investigation-v1":
            raise ValueError("INVESTIGATION_BASELINE_CHANGED")


def correction(budget, request_id):
    with budget.edit() as data:
        progress = data["investigation"]
        existing = progress.get("correction_for")
        if existing and existing != request_id:
            return False
        progress["correction_for"] = request_id
        return True
