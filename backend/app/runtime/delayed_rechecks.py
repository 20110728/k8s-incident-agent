"""One bounded read-only follow-up; terminal publication never launches repair."""
from datetime import UTC, datetime
from types import SimpleNamespace
from contextlib import nullcontext

from backend.app.agent.observation import identity
from backend.app.persistence.delayed_rechecks import WORKFLOW, stale, terminal
from backend.app.persistence.leases import LeaseLost
from backend.app.persistence.runs import request_digest
from backend.app.services.recheck_service import IncidentRecheckService, RecheckRequest
from backend.app.tools.deadline import read_budget
from backend.app.runtime.budget import CURRENT


def target_changed(expected, observed):
    profile = observed.get("service_profile") or {}
    current = {k: profile.get(k) for k in ("digest", "deployment_uid", "deployment_generation")}
    services = [e.get("data", {}) for e in observed.get("evidence", []) if e.get("resource_type") == "Service" and not e.get("error")]
    if len(services) == 1:
        current.update(service_uid=services[0].get("uid"), selector=services[0].get("selector"), ports=services[0].get("ports"))
    return any(value is not None and expected.get(key) is not None and expected[key] != value for key, value in current.items())


def locked_row(repo, lease, conn):
    incident = conn.execute("SELECT * FROM incident_agent_app.incidents WHERE incident_id=%s", (lease["incident_id"],)).fetchone()
    row = conn.execute("""SELECT *,expires_at<=clock_timestamp() AS expired FROM incident_agent_app.delayed_rechecks
        WHERE delayed_id=%s AND run_id=%s AND incident_id=%s FOR UPDATE""",
        (lease["input_payload"]["delayed_id"], lease["run_id"], lease["incident_id"])).fetchone()
    if row is None:
        raise ValueError("DELAYED_TASK_NOT_BOUND")
    if row["status"] == "running":
        invalid = stale(conn, row, incident)
        if invalid or row["expired"]:
            row.update(status="invalidated" if invalid else "expired", reason="BASIS_CHANGED" if invalid else "DELAYED_RECHECK_EXPIRED")
            terminal(conn, row, row["status"], row["reason"])
    return row


def execute_delayed_recheck(repo, lease, lost, *, collector_factory=None, complete=None):
    if (lease["workflow_version"] != WORKFLOW or request_digest(lease["input_payload"]) != lease["input_sha256"]
            or request_digest(lease["context_snapshot"]) != lease["context_sha256"]):
        raise ValueError("INVALID_DELAYED_TASK")

    def owned():
        if lost.is_set():
            raise LeaseLost("worker lost its lease")
        repo.assert_owned(lease)

    owned()
    with repo.fence(lease) as conn:
        row = locked_row(repo, lease, conn)
    if row["status"] == "running":
        if lease.get("recovery_only"):
            with repo.fence(lease) as conn:
                row = locked_row(repo, lease, conn)
                if row["status"] == "running":
                    row.update(status="unknown", reason="ATTEMPTS_EXHAUSTED")
                    terminal(conn, row, row["status"], row["reason"])
        else:
            if collector_factory is None:
                from backend.app.agent.dependencies import build_kubernetes_collector
                collector_factory = lambda: build_kubernetes_collector(bounded_reads=True)
            collector = collector_factory()
            def collect(*args):
                owned()
                return collector.collect(*args)
            frozen = SimpleNamespace(state=row["snapshot"], waiting_for_approval=False)
            service = IncidentRecheckService(SimpleNamespace(get_incident=lambda _: frozen),
                SimpleNamespace(collect=collect), SimpleNamespace(append=lambda _: None))
            remaining = min(30, max(0, (row["expires_at"] - datetime.now(UTC)).total_seconds()))
            budget = CURRENT.get()
            with budget.stage("delayed_recheck", remaining) if budget else nullcontext(), read_budget(remaining):
                observed = service.create(lease["incident_id"], RecheckRequest(note="稳定性窗口通过后的延时只读复查")).model_dump(mode="json")
            owned()
            target = identity(observed["service_profile"], observed["evidence"])
            with repo.fence(lease) as conn:
                row = locked_row(repo, lease, conn)
                if row["status"] == "running":
                    if target_changed(row["target"], observed) or (target is not None and target != row["target"]):
                        status, reason = "invalidated", "TARGET_CHANGED"
                    elif target is None:
                        status, reason = "unknown", "TARGET_UNBOUND"
                    elif observed["status"] == "passed":
                        status, reason = "passed", "DELAYED_SAMPLE_PASSED"
                    elif observed["status"] == "failed":
                        status, reason = "relapsed", "INITIAL_PASS_THEN_FAILURE"
                    else:
                        status, reason = "unknown", "DELAYED_SAMPLE_UNKNOWN"
                    terminal(conn, row, status, reason, observed)
                    row.update(status=status, reason=reason, result=observed)
                else:
                    # Retain actual sampling times even if the deadline/basis
                    # changed during the read; the result remains non-passing.
                    terminal(conn, row, row["status"], row["reason"], observed)
                    row["result"] = observed
    owned()
    labels = {"passed": "延时复查通过", "relapsed": "初次通过后复发", "unknown": "延时复查结果未知",
              "invalidated": "依据或目标变化，延时复查失效", "expired": "延时复查已过期，未验证"}
    output = {"intent": "delayed_recheck", "answer": labels[row["status"]], "delayed_id": row["delayed_id"],
              "delayed_status": row["status"], "cluster_writes_executed": False, "model_called": False,
              "fresh_observation": row["result"] is not None}
    (complete or repo.complete)(lease, output)
