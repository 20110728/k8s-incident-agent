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


def bind_baseline(budget, baseline, version="readonly-investigation-v1"):
    value = digest(baseline)
    with budget.edit() as data:
        saved = data.setdefault("investigation", {"version": version, "baseline_digest": value})
        if saved["baseline_digest"] != value or saved["version"] != version:
            raise ValueError("INVESTIGATION_BASELINE_CHANGED")


def correction(budget, request_id, *, diagnosis=False, tokens=0, seconds=0):
    with budget.edit() as data:
        progress = data["investigation"]
        key = "diagnosis_correction_for" if diagnosis else "correction_for"
        existing = progress.get(key)
        if existing and existing != request_id:
            return False
        if diagnosis and not existing:
            policy = data["policy"]
            if (data["tokens"] + tokens > policy["total_tokens"]
                    or data["seconds"] + seconds > policy["active_seconds"]
                    or sum(c["kind"] == "investigation_model" for c in data["calls"].values()) >= policy.get("model_attempts", 5)):
                return False
        progress[key] = request_id
        return True
