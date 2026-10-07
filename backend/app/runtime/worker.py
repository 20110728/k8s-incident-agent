"""Run with python -m backend.app.runtime.worker. No HTTP process runs jobs."""
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
from backend.app.persistence.leases import LeaseLost, UnsafeRetry
from backend.app.persistence.controls import ControlRepository
from backend.app.persistence.migrations import run_migrations
from backend.app.persistence.runs import request_digest
from backend.app.persistence.settings import get_database_settings
from backend.app.runtime.checkpointer import fenced_checkpointer
from backend.app.runtime.settings import WorkerSettings
from backend.app.runtime.recovery import classify, transient
from backend.app.runtime.operations import LedgerExecutor
from backend.app.tools.client import create_clients
from backend.app.runtime.telemetry import report
from backend.app.runtime.failpoints import get_failpoints, hit
from backend.app.services.round_context import ROUND_WORKFLOWS, DIALOGUE_WORKFLOW
from backend.app.persistence.operations import json_value


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
            started = time.monotonic()
            report("dependency_started", self.lease, node=name, input_value=[args, kwargs])
            try:
                result = method(*args, **kwargs)
            except Exception as error:
                report("dependency_failed", self.lease, node=name,
                       error_class="transient" if transient(error) else "permanent",
                       elapsed_ms=round((time.monotonic()-started)*1000))
                raise
            report("dependency_completed", self.lease, node=name, output_value=result,
                   elapsed_ms=round((time.monotonic()-started)*1000))
            return result
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
            verifier=owned(build_recovery_verifier(repository=repository, lease=lease)),
            checkpointer=saver,
            dialogue=lease["workflow_version"] == DIALOGUE_WORKFLOW,
        )


class Worker:
    def __init__(self, repository, graph_context, settings=None, owner=None, reconciler=None, interaction_handler=None):
        get_failpoints()  # Reject unsafe injection before any worker IO.
        self.repository, self.graph_context = repository, graph_context
        self.settings = settings or WorkerSettings()
        self.owner = owner or str(uuid4())
        self.reconciler = reconciler or self._reconcile_operation
        self.interaction_handler = interaction_handler
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
                report("heartbeat_lost", self._lease, error_class="lease_lost")

    def _finish(self, lease, status, **kwargs):
        with self._lock:
            if self.lost.is_set():
                raise LeaseLost("heartbeat failed")
            status = self.repository.finish(lease, status, **kwargs) or status
            self._lease = None
        report(status, lease, error_code=kwargs.get("error_code"),
               error_class={"retry_scheduled": "transient", "reconciling": "outcome_unknown",
                            "waiting_approval": "needs_input", "waiting_user": "needs_input",
                            "failed": "permanent"}.get(status))

    def _operation(self, lease):
        # Older read-only test repositories have no ledger; production always does.
        reader = getattr(self.repository, "operation", None)
        return reader(lease["run_id"]) if reader else None

    def _complete_interaction(self, lease, output, **kwargs):
        # Coordinate terminal publication with heartbeat, as _finish does.
        with self._lock:
            if self.lost.is_set():
                raise LeaseLost("heartbeat failed")
            self.repository.complete(lease, output, **kwargs)
            self._lease = None

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
                     output_snapshot=json_value(snapshot.values) if snapshot.values else None,
                     question=(snapshot.values or {}).get("question"),
                     adopted_message_ids=[m["message_id"] for m in (snapshot.values or {}).get("round_context", {}).get("messages", [])]
                        + [a["message_id"] for a in (snapshot.values or {}).get("clarification_answers", [])],
                     **({"error_code": decision.error_code} if decision.error_code else {}))

    def execute(self, lease):
        config = {"configurable": {"thread_id": lease["thread_id"]}}
        reading_checkpoint = False
        try:
            if lease.get("run_kind") == "interaction":
                from backend.app.runtime.interactions import execute_interaction
                (self.interaction_handler or execute_interaction)(
                    self.repository, lease, self.lost, complete=self._complete_interaction)
                report("succeeded", lease)
                return
            # Recover external effects even if graph/checkpoint/LLM setup fails.
            if self._reconcile_pending(lease, recovering=True):
                return
            if lease["workflow_version"] not in {"incident-v1", *ROUND_WORKFLOWS} or request_digest(lease["input_payload"]) != lease["input_sha256"]:
                self._finish(lease, "failed", error_code="UNSUPPORTED_OR_CORRUPT_INPUT")
                return
            if lease["workflow_version"] in ROUND_WORKFLOWS and (
                not isinstance(lease.get("context_snapshot"), dict) or
                request_digest(lease["context_snapshot"]) != lease.get("context_sha256")
            ):
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
                report("resume" if decision.action in {"continue", "resume", "resume_user"} else "start", lease)
                self.repository.assert_owned(lease)
                input_value = (Command(resume=lease["answer_payload"]) if decision.action == "resume_user" else
                               Command(resume=lease["approval_payload"]["decision"]) if decision.action == "resume" else
                               None if decision.action == "continue" else
                               {"incident_id": lease["incident_id"], "request": lease["input_payload"]})
                if decision.action == "start" and lease["workflow_version"] in ROUND_WORKFLOWS:
                    input_value["round_context"] = lease["context_snapshot"]
                    input_value["run_id"] = lease["run_id"]
                graph.invoke(input_value, config=config)
                reading_checkpoint = True
                self._project(lease, graph.get_state(config))
        except LeaseLost:
            report("lease_lost", lease)
        except Exception as error:
            try:
                if self._reconcile_pending(lease):
                    return
                if transient(error) and lease["attempt"] - lease.get("attempt_base", 0) < self.settings.max_attempts:
                    self._finish(lease, "retry_scheduled", error_code="DEPENDENCY_TEMPORARY",
                                 retry_seconds=5 if lease["attempt"] == 1 else 15)
                else:
                    code = ("ATTEMPTS_EXHAUSTED" if transient(error) else
                            "CHECKPOINT_UNREADABLE" if reading_checkpoint else "WORKER_FAILED")
                    self._finish(lease, "failed", error_code=code)
            except UnsafeRetry:
                try:
                    self._finish(lease, "reconciling", error_code="WRITE_REQUIRES_RECONCILIATION")
                except Exception:
                    report("result_not_committed", lease)
            except Exception:
                report("result_not_committed", lease)

    def run(self, *, once=False):
        done = threading.Event()
        self.repository.announce(self.owner, self.settings.lease_seconds)
        pulse = threading.Thread(target=self._pulse, args=(done,), daemon=True)
        pulse.start()
        try:
            while not self.stop.is_set():
                activate = getattr(self.repository, "activate_pending", None)
                if activate:
                    activate()
                with self._lock:
                    self.lost.clear()
                    lease = self.repository.claim(self.owner, self.settings.lease_seconds, self.settings.max_attempts)
                    self._lease = lease
                if lease is not None:
                    report("claimed", lease)
                    hit("after_claim", lease)
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
    get_failpoints()
    database = get_database_settings()
    connect = partial(connect_database, database)
    with connect() as connection:
        run_migrations(connection)
    repository = ControlRepository(connect)
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
