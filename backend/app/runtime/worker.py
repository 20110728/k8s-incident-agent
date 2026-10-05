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
from langgraph.types import Command
from backend.app.agent.dependencies import (
    build_kubernetes_collector, build_runbook_retriever,
    build_diagnosis_service, build_remediation_planner, build_recovery_verifier,
)
from backend.app.agent.graph import build_incident_graph
from backend.app.persistence.database import connect_database
from backend.app.persistence.leases import LeaseLost
from backend.app.persistence.operations import OperationRepository
from backend.app.persistence.migrations import run_migrations
from backend.app.persistence.runs import request_digest
from backend.app.persistence.settings import get_database_settings
from backend.app.runtime.checkpointer import fenced_checkpointer
from backend.app.runtime.settings import WorkerSettings
from backend.app.runtime.recovery import classify, transient
from backend.app.runtime.operations import LedgerExecutor
from backend.app.tools.client import create_clients


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
            executor=owned(LedgerExecutor(create_clients(disable_retries=True), repository, lease, lost)),
            verifier=owned(build_recovery_verifier()),
            checkpointer=saver,
        )


class Worker:
    def __init__(self, repository, graph_context, settings=None, owner=None, reconciler=None):
        self.repository, self.graph_context = repository, graph_context
        self.settings = settings or WorkerSettings()
        self.owner = owner or str(uuid4())
        self.reconciler = reconciler or self._reconcile_operation
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

    def _operation(self, lease):
        # Older read-only test repositories have no ledger; production always does.
        reader = getattr(self.repository, "operation", None)
        return reader(lease["run_id"]) if reader else None

    def _reconcile_operation(self, lease, operation):
        return LedgerExecutor(create_clients(disable_retries=True), self.repository, lease).reconcile(operation)

    def _reconcile_pending(self, lease, *, recovering=False):
        operation = self._operation(lease)
        if not operation:
            return False
        uncertain = operation["state"] in {"dispatching", "outcome_unknown", "manual_required"}
        saved_response = (recovering and operation["state"] in {"succeeded", "reconciled"}
                          and operation["lease_epoch"] != lease["lease_epoch"])
        if not uncertain and not saved_response:
            return False
        self.repository.assert_owned(lease)
        try:
            confirmed = self.reconciler(lease, operation)
        except LeaseLost:
            raise
        except Exception:
            self.repository.record(lease, "manual_required", code="RECONCILIATION_READ_FAILED")
            confirmed = False
        if not confirmed or uncertain:
            self._finish(lease, "reconciling", error_code="OPERATION_MANUAL_REQUIRED")
            return True
        return False

    def _project(self, lease, snapshot):
        if self._reconcile_pending(lease):
            return
        decision = classify(lease, snapshot, self._operation(lease))
        if decision.action != "stop":
            self._finish(lease, "failed", error_code="INCOMPLETE_WORKFLOW")
            return
        self._finish(lease, decision.status, phase=str((snapshot.values or {}).get("phase") or "failed"),
                     **({"error_code": decision.error_code} if decision.error_code else {}))

    def execute(self, lease):
        config = {"configurable": {"thread_id": lease["thread_id"]}}
        reading_checkpoint = False
        try:
            # Recover external effects even if graph/checkpoint/LLM setup fails.
            if self._reconcile_pending(lease, recovering=True):
                return
            if lease["workflow_version"] != "incident-v1" or request_digest(lease["input_payload"]) != lease["input_sha256"]:
                self._finish(lease, "failed", error_code="UNSUPPORTED_OR_CORRUPT_INPUT")
                return
            with self.graph_context(lease, self.lost) as graph:
                self.repository.assert_owned(lease)
                reading_checkpoint = True
                snapshot = graph.get_state(config)
                reading_checkpoint = False
                decision = classify(lease, snapshot, self._operation(lease))
                if decision.action == "stop":
                    self._project(lease, snapshot)
                    return
                if lease.get("recovery_only"):
                    self._finish(lease, "failed", error_code="ATTEMPTS_EXHAUSTED")
                    return
                report("resume" if decision.action in {"continue", "resume"} else "start", lease)
                self.repository.assert_owned(lease)
                input_value = (Command(resume=lease["approval_payload"]["decision"]) if decision.action == "resume" else
                               None if decision.action == "continue" else
                               {"incident_id": lease["incident_id"], "request": lease["input_payload"]})
                graph.invoke(input_value, config=config)
                reading_checkpoint = True
                self._project(lease, graph.get_state(config))
        except LeaseLost:
            report("lease_lost", lease)
        except Exception as error:
            try:
                if self._reconcile_pending(lease):
                    return
                if transient(error) and lease["attempt"] < self.settings.max_attempts:
                    self._finish(lease, "retry_scheduled", error_code="DEPENDENCY_TEMPORARY",
                                 retry_seconds=5 if lease["attempt"] == 1 else 15)
                else:
                    code = ("ATTEMPTS_EXHAUSTED" if transient(error) else
                            "CHECKPOINT_UNREADABLE" if reading_checkpoint else "WORKER_FAILED")
                    self._finish(lease, "failed", error_code=code)
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
    repository = OperationRepository(connect)
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
