from contextlib import contextmanager
from types import SimpleNamespace
import threading

import pytest

from backend.app.persistence.leases import LeaseLost
from backend.app.persistence.runs import request_digest, QueuedExecutionUnavailable
from backend.app.runtime.settings import WorkerSettings
from backend.app.runtime.worker import Worker, NoQueuedWrites, OwnedDependency


class Repository:
    def __init__(self):
        self.results = []
    def assert_owned(self, lease):
        pass
    def finish(self, lease, status, **kwargs):
        self.results.append((status, kwargs))


def lease():
    payload = {"namespace": "default", "service_name": "demo", "description": "test"}
    return dict(run_id="run", incident_id="incident", thread_id="thread", lease_epoch=1,
                workflow_version="incident-v1", input_payload=payload, input_sha256=request_digest(payload), attempt=1)


def test_invalid_heartbeat_configuration_rejected():
    with pytest.raises(ValueError):
        WorkerSettings(_env_file=None, heartbeat_seconds=5, lease_seconds=5)


def test_transient_error_is_bounded_and_does_not_delete_task():
    repo = Repository()
    @contextmanager
    def unavailable(*args):
        raise TimeoutError()
        yield
    worker = Worker(repo, unavailable, WorkerSettings(_env_file=None))
    row = lease()
    worker.execute(row)
    assert repo.results[-1] == ("retry_scheduled", {"error_code": "DEPENDENCY_TEMPORARY", "retry_seconds": 5})
    row["attempt"] = 2
    worker.execute(row)
    assert repo.results[-1][1]["retry_seconds"] == 15
    row["attempt"] = 3
    worker.execute(row)
    assert repo.results[-1] == ("failed", {"error_code": "ATTEMPTS_EXHAUSTED"})


def test_business_failed_end_is_not_automatically_retried():
    repo = Repository()
    snapshot = SimpleNamespace(values={"incident_id": "incident", "phase": "diagnosis_failed"}, next=(), tasks=())
    Worker(repo, None)._project(lease(), snapshot)
    assert repo.results == [("failed", {"phase": "diagnosis_failed", "error_code": "WORKFLOW_FAILED"})]


def test_stale_worker_cannot_even_enter_write_dependency():
    class Tool:
        def execute(self):
            pytest.fail("write entered after lease loss")
    lost = threading.Event()
    lost.set()
    with pytest.raises(LeaseLost):
        OwnedDependency(Tool(), Repository(), lease(), lost).execute()
    with pytest.raises(QueuedExecutionUnavailable):
        NoQueuedWrites().execute()


def test_heartbeat_failure_never_resumes_renewal_of_same_lease():
    class BrokenOnce(Repository):
        announcements = 0
        renewals = 0
        def announce(self, owner, seconds):
            self.announcements += 1
            if self.announcements == 1:
                raise ConnectionError()
        def heartbeat(self, row, seconds):
            self.renewals += 1
    class TwoPulses:
        ticks = 0
        def wait(self, seconds):
            self.ticks += 1
            return self.ticks > 2
    repo = BrokenOnce()
    worker = Worker(repo, None)
    worker._lease = lease()
    worker._pulse(TwoPulses())
    assert worker.lost.is_set()
    assert repo.announcements == 2 and repo.renewals == 0
