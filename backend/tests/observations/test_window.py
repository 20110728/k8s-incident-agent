from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.app.agent.observation import ObservationWindow, identity
from backend.app.tools.deadline import BoundedApi, read_budget


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class MemoryStore:
    def __init__(self):
        self.value = None

    def open(self, initial):
        if self.value is None:
            self.value = deepcopy(initial)
        return deepcopy(self.value)

    def save(self, value):
        self.value = deepcopy(value)


def window(store, clock, **kwargs):
    return ObservationWindow(store, clock=clock, monotonic=clock, sleep=clock.sleep, **kwargs)


def observation(status="passed", target="uid-1", resource="ready"):
    return {"status": status, "resource_status": resource, "business_status": status,
            "target": {"uid": target} if target else None, "result": {"sample": status}}


@pytest.mark.parametrize("middle", ["failed", "unknown"])
def test_interrupted_streak_needs_three_new_passes(middle):
    store, clock = MemoryStore(), Clock()
    states = iter(["passed", middle, "passed", "passed", "passed"])
    result = window(store, clock).run(lambda _: observation(next(states)))
    assert result["status"] == "passed" and len(result["samples"]) == 5
    assert result["consecutive"] == 3
    assert all(b["finished_epoch"] - a["finished_epoch"] >= 5 for a, b in zip(result["samples"], result["samples"][1:]))


def test_target_change_invalidates_instead_of_mixing_successes():
    targets = iter(["old", "new"])
    result = window(MemoryStore(), Clock()).run(lambda _: observation(target=next(targets)))
    assert result["status"] == "invalidated" and result["consecutive"] == 0


@pytest.mark.parametrize("sample", [observation(target=None), observation("unknown"), observation("failed", resource="not_ready")])
def test_missing_binding_unknown_and_unready_never_pass(sample):
    clock = Clock()
    result = window(MemoryStore(), clock).run(lambda _: sample)
    assert result["status"] == "unknown" and clock.now <= 1120


def test_resource_relapse_after_first_pass_does_not_extend_business_budget():
    clock, count = Clock(), 0
    def sample(_):
        nonlocal count
        count += 1
        return observation() if count == 1 else observation("failed", resource="not_ready")
    result = window(MemoryStore(), clock).run(sample)
    assert result["status"] == "unknown" and clock.now == 1030
    assert result["consecutive"] == 0


def test_late_result_cannot_turn_timeout_into_success():
    clock = Clock()
    def slow(seconds):
        clock.sleep(seconds + 1)
        return observation()
    result = window(MemoryStore(), clock).run(slow)
    assert result["status"] == "unknown" and result["reason"] == "DEADLINE_EXCEEDED"
    assert len(result["samples"]) == 1


def test_first_business_sample_is_charged_to_its_budget():
    clock = Clock()
    def slow(_):
        clock.sleep(31)
        return observation()
    result = window(MemoryStore(), clock).run(slow)
    assert result["status"] == "unknown" and result["business_deadline"] == 1030


def test_restart_records_interruption_resets_streak_and_keeps_budget():
    store, clock = MemoryStore(), Clock()
    count = 0
    def crash(_):
        nonlocal count
        count += 1
        if count == 3:
            raise SystemExit("process died")
        return observation()
    with pytest.raises(SystemExit):
        window(store, clock).run(crash)
    deadline = store.value["deadline"]
    result = window(store, clock).run(lambda _: observation())
    assert result["deadline"] == deadline
    assert result["samples"][2]["status"] == "interrupted"
    assert result["consecutive"] == 3 and len(result["samples"]) == 6
    # Terminal replay never makes another observation.
    assert window(store, clock).run(lambda _: pytest.fail("terminal resampled")) == result


def test_downtime_consumes_original_budget():
    store, clock = MemoryStore(), Clock()
    with pytest.raises(SystemExit):
        window(store, clock).run(lambda _: (_ for _ in ()).throw(SystemExit()))
    clock.sleep(121)
    result = window(store, clock).run(lambda _: pytest.fail("expired window sampled"))
    assert result["status"] == "unknown" and result["deadline"] == 1120


def test_identity_requires_service_uid_and_tracks_critical_deployment_fields():
    profile = {"status": "matched", "digest": "profile-v1", "deployment_uid": "dep", "deployment_generation": 2}
    evidence = [{"resource_type": "Service", "data": {"uid": "svc", "selector": {"app": "one"}, "ports": [80]}}]
    assert identity(profile, evidence)["deployment_generation"] == 2
    evidence[0]["data"].pop("uid")
    assert identity(profile, evidence) is None


def test_request_timeouts_are_bounded_and_no_call_starts_after_deadline(monkeypatch):
    import backend.app.tools.deadline as module
    clock, calls = Clock(), []
    monkeypatch.setattr(module.time, "monotonic", clock)
    api = BoundedApi(SimpleNamespace(read=lambda **kwargs: calls.append(kwargs)))
    with read_budget(4):
        api.read(_request_timeout=(3, 10))
        assert calls[0]["_request_timeout"] == (2, 2)
        clock.sleep(5)
        with pytest.raises(TimeoutError):
            api.read()
    assert len(calls) == 1
