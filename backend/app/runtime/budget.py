"""Durable conservative reservations. Lost calls retain their full reservation."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from math import ceil
import json
import time
from uuid import uuid4

from psycopg.types.json import Jsonb

CURRENT = ContextVar("run_budget", default=None)
POLICY = {"version": "investigation-budget-v1", "active_seconds": 300, "extra_seconds": 90,
          "decisions": 3, "tools": 6, "input_tokens": 12000, "output_tokens": 2000, "total_tokens": 40000,
          "write_reserve_seconds": 150}


class BudgetExceeded(RuntimeError):
    pass


@contextmanager
def bind_budget(budget):
    token = CURRENT.set(budget)
    try:
        yield budget
    finally:
        CURRENT.reset(token)


class RunBudget:
    def __init__(self, repo, lease):
        self.repo, self.lease = repo, lease
        self.measured_seconds = 0

    @contextmanager
    def edit(self):
        with self.repo.fence(self.lease) as conn:
            initial = {"policy": POLICY, "seconds": 0, "extra_seconds": 0, "tokens": 0,
                       "decisions": [], "tools": [], "calls": {}, "references": {}, "exhausted": None}
            conn.execute("INSERT INTO incident_agent_app.run_budgets(run_id,payload) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                         (self.lease["run_id"], Jsonb(initial)))
            data = conn.execute("SELECT payload FROM incident_agent_app.run_budgets WHERE run_id=%s FOR UPDATE", (self.lease["run_id"],)).fetchone()["payload"]
            yield data
            conn.execute("UPDATE incident_agent_app.run_budgets SET payload=%s WHERE run_id=%s", (Jsonb(data), self.lease["run_id"]))

    def reserve(self, kind, seconds=0, *, extra=False, tokens=0, key=None, metadata=None):
        if seconds < 0 or tokens < 0 or (kind == "tool" and not key):
            raise ValueError("INVALID_BUDGET_RESERVATION")
        error = None
        ticket = str(uuid4())
        with self.edit() as data:
            policy = data["policy"]
            if kind == "tool" and key in data["tools"]:
                error = "DUPLICATE_TOOL_EVIDENCE"
            elif kind == "tool" and len(data["tools"]) >= policy["tools"]:
                error = "TOOL_REQUEST_LIMIT"
            elif (data["seconds"] + seconds > policy["active_seconds"] or
                  extra and data["extra_seconds"] + seconds > policy["extra_seconds"]):
                error = "ACTIVE_TIME_LIMIT"
            elif data["tokens"] + tokens > policy["total_tokens"]:
                error = "MODEL_TOKEN_LIMIT"
            if error:
                data["exhausted"] = error
            else:
                data["seconds"] += seconds
                data["extra_seconds"] += seconds if extra else 0
                data["tokens"] += tokens
                if kind == "tool":
                    data["tools"].append(key)
                data["calls"][ticket] = {"kind": kind, "reserved_seconds": seconds, "extra": extra,
                    "reserved_tokens": tokens, "status": "started_or_interrupted", "metadata": metadata or {}}
        if error:
            raise BudgetExceeded(error)
        return ticket

    def settle(self, ticket, elapsed, *, tokens=None, usage=None, status="completed", result=None):
        with self.edit() as data:
            call = data["calls"][ticket]
            if call["status"] != "started_or_interrupted":
                return
            # Do not hide a call that exceeded its network timeout.
            elapsed = max(0, elapsed)
            data["seconds"] += elapsed - call["reserved_seconds"]
            if call["extra"]:
                data["extra_seconds"] += elapsed - call["reserved_seconds"]
            if tokens is not None:
                data["tokens"] += tokens - call["reserved_tokens"]
            call.update(status=status, elapsed_seconds=elapsed, usage=usage, charged_tokens=tokens if tokens is not None else call["reserved_tokens"])
            if result is not None:
                call["result"] = result
            if data["seconds"] > data["policy"]["active_seconds"]:
                data["exhausted"] = "ACTIVE_TIME_LIMIT"
        if call["kind"] != "control_overhead":
            self.measured_seconds += elapsed

    def deny(self, reason):
        with self.edit() as data:
            data["exhausted"] = reason
        raise BudgetExceeded(reason)

    def decision(self, key):
        error = None
        with self.edit() as data:
            if key in data["decisions"]:
                return  # Replayed interrupt preparation is not a new decision.
            if len(data["decisions"]) >= data["policy"]["decisions"]:
                error = data["exhausted"] = "INVESTIGATION_DECISION_LIMIT"
            else:
                data["decisions"].append(key)
        if error:
            raise BudgetExceeded(error)

    def before_write(self, required=None):
        error = None
        with self.edit() as data:
            if (data["policy"]["active_seconds"] - data["seconds"] < (required or data["policy"]["write_reserve_seconds"])
                    or data["tokens"] > data["policy"]["total_tokens"]):
                error = data["exhausted"] = "WRITE_VERIFICATION_BUDGET_NOT_RESERVED"
        if error:
            raise BudgetExceeded(error)

    @contextmanager
    def stage(self, name, seconds, *, extra=False):
        from backend.app.tools.deadline import read_budget
        ticket = self.reserve(name, seconds, extra=extra)
        start = time.monotonic()
        status = "failed_or_unknown"
        try:
            with read_budget(seconds):
                yield
            status = "completed"
        finally:
            elapsed = time.monotonic() - start
            self.settle(ticket, elapsed, status=status)

    @contextmanager
    def activity(self):
        """Charge graph/control overhead too, excluding completed dependency stages.

        Permit cached result publication even at zero remaining budget. New I/O
        still requires a reservation. A crash retains five seconds of overhead.
        """
        ticket = str(uuid4())
        with self.edit() as data:
            data["seconds"] += 5
            data["calls"][ticket] = {"kind": "control_overhead", "reserved_seconds": 5, "extra": False,
                                    "reserved_tokens": 0, "status": "started_or_interrupted", "metadata": {}}
        start, before = time.monotonic(), self.measured_seconds
        try:
            yield
        finally:
            elapsed = max(0, time.monotonic() - start - (self.measured_seconds - before))
            self.settle(ticket, elapsed)

    def references(self, values=None):
        with self.edit() as data:
            if values:
                for key, value in values.items():
                    if key in data["references"] and data["references"][key] != value:
                        raise ValueError("RESOURCE_REFERENCE_CHANGED")
                    data["references"][key] = value
            return deepcopy(data["references"])


def invoke_model(runnable, messages, schema):
    budget = CURRENT.get()
    if budget is None:
        return runnable.invoke(messages)
    # Provider-independent estimate, not an exact Qwen tokenizer or price claim.
    text = json.dumps([getattr(m, "content", m) for m in messages], ensure_ascii=False, default=str)
    schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    estimated = ceil(len((text + schema_text).encode("utf-8")) / 3) + 512
    if estimated > POLICY["input_tokens"]:
        budget.deny("MODEL_INPUT_ESTIMATE_LIMIT")
    ticket = budget.reserve("model", tokens=POLICY["input_tokens"] + POLICY["output_tokens"],
        metadata={"input_estimate": estimated, "estimate_source": "utf8_bytes_div_3_plus_schema_and_512", "input_limit_is_estimated": True})
    try:
        response = runnable.invoke(messages)
    except Exception:
        budget.settle(ticket, 0, status="failed_or_unknown")
        raise
    raw = response.get("raw") if isinstance(response, dict) else response
    usage = getattr(raw, "usage_metadata", None) or {}
    actual = usage.get("total_tokens")
    if type(actual) is not int or actual <= 0:
        actual = None
    budget.settle(ticket, 0, tokens=actual, usage=usage or None)
    return response


def budget_view(repo, incident_id, run_id):
    repo.get_round(incident_id, run_id)  # Do not expose another incident's run.
    rows = repo._read("SELECT payload FROM incident_agent_app.run_budgets WHERE run_id=%s", (run_id,))
    if not rows:
        return {"available": False, "run_id": run_id}
    data = rows[0]["payload"]
    policy = data["policy"]
    return {"available": True, "run_id": run_id, "policy": policy,
        "used": {"active_seconds": data["seconds"], "extra_seconds": data["extra_seconds"], "tokens": data["tokens"],
                 "decisions": len(data["decisions"]), "tools": len(data["tools"])},
        "exhausted": data["exhausted"], "calls": list(data["calls"].values()),
        "handoff": {"reason": data["exhausted"],
            "known": "已完成的采集与诊断仍在本轮记录中；预算拒绝不会证明故障已消失。",
            "unknown": "未完成、失败或截断的采集不能证明目标健康；丢失 usage 的模型调用保留预留额度。",
            "next_step": "核对本轮证据与操作账本，必要时人工排查或明确发起新一轮调查。"} if data["exhausted"] else None}
