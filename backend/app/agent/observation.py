"""Versioned bounded observations. Restart resets streak, never the deadline."""
from copy import deepcopy
from datetime import UTC, datetime
import time
from uuid import uuid4

POLICY = {"version": "recovery-window-v1", "resource_seconds": 60,
          "business_seconds": 30, "total_seconds": 120, "interval_seconds": 5,
          "required_consecutive": 3, "max_samples": 25}


def timestamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


def identity(profile, evidence):
    services = [e.get("data", {}) for e in evidence if e.get("resource_type") == "Service" and not e.get("error")]
    if profile.get("status") != "matched" or len(services) != 1:
        return None
    service = services[0]
    result = {k: profile.get(k) for k in ("digest", "deployment_uid", "deployment_generation")}
    result.update(service_uid=service.get("uid"), selector=service.get("selector"), ports=service.get("ports"))
    return result if all(v is not None for v in result.values()) and result["service_uid"] and result["deployment_uid"] else None


class ObservationWindow:
    def __init__(self, store, *, clock=time.time, monotonic=time.monotonic, sleep=time.sleep, policy=None):
        self.store, self.clock, self.monotonic, self.sleep = store, clock, monotonic, sleep
        self.policy = dict(policy or POLICY)

    def run(self, sample):
        started = self.clock()
        initial = {"policy": self.policy, "started_at": timestamp(started),
                   "deadline": started + self.policy["total_seconds"],
                   "resource_deadline": started + self.policy["resource_seconds"],
                   "business_deadline": None, "samples": [], "consecutive": 0,
                   "status": "running", "target": None, "last_result": None}
        window = self.store.open(initial)
        if window["status"] != "running":
            return window
        policy = window["policy"]
        if policy["version"] != POLICY["version"]:
            raise ValueError("UNSUPPORTED_VERIFICATION_POLICY")
        # Each invocation is a new uninterrupted segment. In-flight samples from
        # a crashed worker remain visible but cannot count as successful.
        segment = str(uuid4())
        window["consecutive"] = 0
        for item in window["samples"]:
            if item["status"] == "sampling":
                item.update(status="interrupted", finished_at=None)
        self.store.save(window)
        mono_deadline = self.monotonic() + max(0, window["deadline"] - self.clock())

        def remaining():
            phase_deadline = window["business_deadline"] or window["resource_deadline"]
            return min(window["deadline"] - self.clock(), phase_deadline - self.clock(),
                       mono_deadline - self.monotonic())

        while remaining() > 0 and len(window["samples"]) < policy["max_samples"]:
            if window["samples"]:
                # Gap is measured from previous completion, not request start.
                last = window["samples"][-1]
                delay = max(0, last.get("finished_epoch", self.clock()) + policy["interval_seconds"] - self.clock())
                self.sleep(min(delay, max(0, remaining())))
            if remaining() <= 0:
                break
            item = {"sequence": len(window["samples"]) + 1, "segment": segment,
                    "started_at": timestamp(self.clock()), "started_epoch": self.clock(),
                    "finished_at": None, "status": "sampling"}
            window["samples"].append(item)
            self.store.save(window)  # Must persist BEFORE external reads.
            observed = sample(max(0, min(remaining(), policy["business_seconds"])))
            finished = self.clock()
            item.update({k: observed.get(k) for k in ("status", "resource_status", "business_status", "target", "error_code")})
            item.update(finished_at=timestamp(finished), finished_epoch=finished)
            window["last_result"] = observed.get("result")
            late = remaining() <= 0
            if observed.get("resource_status") == "ready" and window["business_deadline"] is None:
                # Charge the first business sample too (conservatively including
                # its resource reads); never grant 30 extra seconds after it.
                window["business_deadline"] = min(window["deadline"], item["started_epoch"] + policy["business_seconds"])
                late = late or remaining() <= 0
            target = observed.get("target")
            if observed.get("invalidated") or (target and window["target"] and target != window["target"]):
                window.update(status="invalidated", reason="TARGET_CHANGED", consecutive=0)
            elif late:
                window.update(status="unknown", reason="DEADLINE_EXCEEDED", consecutive=0)
            else:
                if target:
                    window["target"] = target
                passed = (observed.get("status") == "passed" and observed.get("resource_status") == "ready"
                          and observed.get("business_status") == "passed" and target is not None)
                window["consecutive"] = window["consecutive"] + 1 if passed else 0
                if window["consecutive"] >= policy["required_consecutive"]:
                    window.update(status="passed", reason="CONSECUTIVE_SAMPLES_PASSED")
            if window["status"] != "running":
                window["finished_at"] = timestamp(finished)
            self.store.save(window)
            if window["status"] != "running":
                break
        if window["status"] == "running":
            window.update(status="unknown", reason="INSUFFICIENT_STABLE_SAMPLES")
        window["finished_at"] = timestamp(self.clock())
        self.store.save(window)
        return window


def summary(window):
    return deepcopy({key: value for key, value in window.items() if key != "last_result"})
