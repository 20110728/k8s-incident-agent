"""Run with python -m backend.app.runtime.worker. No HTTP process runs jobs."""
import json
import os
import signal
import threading
import time
from contextlib import contextmanager
from functools import partial
from uuid import uuid4

from backend.app.persistence import serialization_security  # strict serde before graph imports
from backend.app.agent.dependencies import (
    build_kubernetes_collector, build_runbook_retriever,
    build_diagnosis_service, build_remediation_planner,
)
from backend.app.agent.graph import build_incident_graph
from backend.app.persistence.database import connect_database
from backend.app.persistence.leases import LeaseLost, LeaseRepository
from backend.app.persistence.migrations import run_migrations
from backend.app.persistence.runs import QueuedExecutionUnavailable, request_digest
from backend.app.persistence.settings import get_database_settings
from backend.app.runtime.checkpointer import fenced_checkpointer
from backend.app.runtime.settings import WorkerSettings


def report(event, lease=None):
    # No input, DSN, dependency exception or model text in worker logs.
    print(json.dumps({"event": event, "run_id": lease["run_id"] if lease else None,
                      "epoch": lease["lease_epoch"] if lease else None}), flush=True)


class OwnedDependency:
    def __init__(self, dependency, repository, lease, lost):
        self.dependency, self.repository = dependency, repository
        self.lease, self.lost = lease, lost

    def __getattr__(self, name):
        method = getattr(self.dependency, name)
        def call(*args, **kwargs):
            if self.lost.is_set():
                raise LeaseLost("worker lost its lease")
            self.repository.assert_owned(self.lease)
            return method(*args, **kwargs)
        return call


class NoQueuedWrites:
    """Keep the complete graph topology, but wait for 2B's operation ledger."""
    def execute(self, *args, **kwargs):
        raise QueuedExecutionUnavailable()

    def verify(self, *args, **kwargs):
        raise QueuedExecutionUnavailable()


@contextmanager
def production_graph(settings, repository, lease, lost):
    with fenced_checkpointer(settings, repository, lease, lost) as saver:
        def owned(dependency):
            return OwnedDependency(dependency, repository, lease, lost)
        yield build_incident_graph(
            collector=owned(build_kubernetes_collector()),
            retriever=owned(build_runbook_retriever()),
            diagnoser=owned(build_diagnosis_service()),
            planner=owned(build_remediation_planner()),
            executor=owned(NoQueuedWrites()), verifier=owned(NoQueuedWrites()),
            checkpointer=saver,
        )


class Worker:
    def __init__(self, repository, graph_context, settings=None, owner=None):
        self.repository, self.graph_context = repository, graph_context
        self.settings = settings or WorkerSettings()
        self.owner = owner or str(uuid4())
        self.stop = threading.Event()
        self.lost = threading.Event()
        self._lease = None
        self._lock = threading.Lock()

    def _pulse(self, done):
        while not done.wait(self.settings.heartbeat_seconds):
            try:
                self.repository.announce(self.owner, self.settings.lease_seconds)
                with self._lock:
                    lease = self._lease
                    if lease is not None and not self.lost.is_set():
                        self.repository.heartbeat(lease, self.settings.lease_seconds)
            except Exception:
                # Fail closed; a recovered DB connection cannot resurrect ownership.
                self.lost.set()
                report("heartbeat_lost")

    def _finish(self, lease, status, **kwargs):
        with self._lock:
            if self.lost.is_set():
                raise LeaseLost("heartbeat failed")
            self.repository.finish(lease, status, **kwargs)
            self._lease = None
        report(status, lease)

    def _project(self, lease, snapshot):
        state = snapshot.values or {}
        if state.get("incident_id") != lease["incident_id"]:
            self._finish(lease, "failed", error_code="CHECKPOINT_IDENTITY_MISMATCH")
            return
        phase = str(state.get("phase") or "unknown")
        interrupts = any(getattr(task, "interrupts", ()) for task in snapshot.tasks)
        if interrupts and phase == "awaiting_approval" and state.get("approval_status") == "pending":
            self._finish(lease, "waiting_approval", phase=phase)
        elif snapshot.next:
            # 2A owns recovery classification; never blindly invoke(None) here.
            self._finish(lease, "failed", phase=phase, error_code="CHECKPOINT_REQUIRES_REVIEW")
        elif phase in {"remediation_skipped", "remediation_planned"}:
            self._finish(lease, "succeeded", phase=phase)
        else:
            self._finish(lease, "failed", phase=phase, error_code="WORKFLOW_FAILED")

    def execute(self, lease):
        config = {"configurable": {"thread_id": lease["thread_id"]}}
        try:
            if lease["workflow_version"] != "incident-v1" or request_digest(lease["input_payload"]) != lease["input_sha256"]:
                self._finish(lease, "failed", error_code="UNSUPPORTED_OR_CORRUPT_INPUT")
                return
            with self.graph_context(lease, self.lost) as graph:
                self.repository.assert_owned(lease)
                snapshot = graph.get_state(config)
                if snapshot.values or snapshot.next or (snapshot.config or {}).get("configurable", {}).get("checkpoint_id"):
                    self._project(lease, snapshot)
                    return
                if lease["attempt"] > 1:
                    # Safe only because this release never dispatches queued writes.
                    report("restart_without_checkpoint", lease)
                graph.invoke({"incident_id": lease["incident_id"], "request": lease["input_payload"]}, config=config)
                self._project(lease, graph.get_state(config))
        except LeaseLost:
            report("lease_lost", lease)
        except (TimeoutError, ConnectionError):
            try:
                if lease["attempt"] < self.settings.max_attempts:
                    self._finish(lease, "retry_scheduled", error_code="DEPENDENCY_TEMPORARY",
                                 retry_seconds=5 if lease["attempt"] == 1 else 15)
                else:
                    self._finish(lease, "failed", error_code="ATTEMPTS_EXHAUSTED")
            except Exception:
                report("result_not_committed", lease)
        except Exception:
            try:
                self._finish(lease, "failed", error_code="WORKER_FAILED")
            except Exception:
                report("result_not_committed", lease)

    def run(self, *, once=False):
        done = threading.Event()
        self.repository.announce(self.owner, self.settings.lease_seconds)
        pulse = threading.Thread(target=self._pulse, args=(done,), daemon=True)
        pulse.start()
        try:
            while not self.stop.is_set():
                with self._lock:
                    self.lost.clear()
                    lease = self.repository.claim(self.owner, self.settings.lease_seconds, self.settings.max_attempts)
                    self._lease = lease
                if lease is not None:
                    report("claimed", lease)
                    task = threading.Thread(target=self.execute, args=(lease,), daemon=True)
                    task.start()
                    deadline = None
                    while task.is_alive():
                        task.join(timeout=0.1)
                        if self.stop.is_set():
                            deadline = deadline or time.monotonic() + self.settings.shutdown_seconds
                            if time.monotonic() >= deadline:
                                self.lost.set()
                                report("shutdown_deadline", lease)
                                return
                    with self._lock:
                        self._lease = None
                if once:
                    return
                self.stop.wait(self.settings.poll_seconds)
        finally:
            done.set()
            pulse.join(timeout=6)
            try:
                self.repository.withdraw(self.owner)
            except Exception:
                pass


def main():
    database = get_database_settings()
    connect = partial(connect_database, database)
    with connect() as connection:
        run_migrations(connection)
    repository = LeaseRepository(connect)
    worker = Worker(repository, partial(production_graph, database, repository))
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: worker.stop.set())
    try:
        worker.run()
    except Exception:
        report("worker_stopped_with_error")
        raise SystemExit(1) from None
    # Synchronous SDK calls cannot be cancelled by Python; after the graceful
    # window exit the whole process. Never release a still-running lease early.
    os._exit(0)


if __name__ == "__main__":
    main()
