"""R01-R06: process boundaries + retained PostgreSQL and real kind evidence."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

import httpx
import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from backend.app.agent.approval import build_approval_request
from backend.app.persistence.leases import LeaseLost
from backend.app.persistence.operations import approval_binding
from backend.app.service_profiles.models import ServiceProfile
from backend.app.service_profiles.registry import profile_digest
from backend.app.tools.client import create_clients, REQUEST_TIMEOUT
from backend.tests.runtime.process_driver import service_snapshot
from backend.tests.runtime.test_operations_postgres import state_with_uid, decision_for, waiting
from backend.tests.runtime.test_worker_postgres import storage, accept, wait_until

pytestmark = pytest.mark.skipif(os.environ.get("STAGE2C_RECOVERY") != "1", reason="requires 2C ECS process acceptance")


class Lab:
    def __init__(self, storage, directory):
        self.connect, self.repo, self.settings = storage
        self.directory, self.processes = directory, []
        self.state = None

    def start(self, action="worker", *, graph="readonly", point=None, mode="pause"):
        directory = self.directory / f"{len(self.processes):02d}-{action}"
        directory.mkdir()
        if self.state:
            (directory / "state.json").write_text(json.dumps(self.state), encoding="utf-8")
        environment = dict(os.environ, PGVECTOR_URL=self.settings.database_url.get_secret_value(),
                           INCIDENT_AGENT_API_ENVIRONMENT="test", INCIDENT_AGENT_EXECUTION_MODE="queued")
        environment.update(INCIDENT_AGENT_TEST_FAILPOINT=point or "",
                           INCIDENT_AGENT_TEST_FAILPOINT_MODE=mode,
                           INCIDENT_AGENT_TEST_BARRIER_DIR=str(directory))
        with (directory / "timeline.jsonl").open("w", encoding="utf-8") as output:
            process = subprocess.Popen([sys.executable, "-m", "backend.tests.runtime.process_driver", action,
                                        str(directory), "--graph", graph], env=environment, stdout=output, stderr=output)
        process.case_directory = directory
        self.processes.append(process)
        return process

    def barrier(self, process, point):
        path = process.case_directory / (point + ".reached")
        wait_until(lambda: path.exists() or process.poll() is not None, timeout=30)
        assert path.exists(), f"child exited before {point}; see {process.case_directory}/timeline.jsonl"

    def release(self, process, point):
        (process.case_directory / (point + ".release")).write_text("release", encoding="utf-8")

    def join(self, process):
        assert process.wait(timeout=40) == 0, f"child failed; see {process.case_directory}/timeline.jsonl"

    def api(self, *, point=None):
        process = self.start("api", point=point)
        file = process.case_directory / "api-port.json"
        wait_until(lambda: file.exists() or process.poll() is not None, timeout=30)
        assert file.exists(), f"API startup failed; see {process.case_directory}"
        address = "http://127.0.0.1:" + str(json.loads(file.read_text())["port"])
        def ready():
            try:
                return httpx.get(address + "/readyz", timeout=1, trust_env=False).status_code == 200
            except httpx.TransportError:
                return False
        wait_until(ready, timeout=30)
        return process, address

    def kill(self, process):
        process.kill()
        assert process.wait(timeout=10) == -signal.SIGKILL
        self.save("kill-" + str(process.pid), {"signal": "SIGKILL", "pid": process.pid, "time_ns": time.time_ns()})

    def expired(self, run_id):
        def expired():
            with self.connect() as connection:
                return connection.execute("SELECT lease_expires_at<=clock_timestamp() AS expired FROM incident_agent_app.runs WHERE run_id=%s", (run_id,)).fetchone()["expired"]
        wait_until(expired, timeout=15)

    def save(self, name, value):
        (self.directory / (name + ".json")).write_text(json.dumps(value, default=str, indent=2), encoding="utf-8")

    def requests(self):
        entries = []
        for path in self.directory.glob("*/api.jsonl"):
            entries.extend(json.loads(line) for line in path.read_text().splitlines())
        return [entry for entry in entries if entry["event"] == "request"]

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


@pytest.fixture
def lab(storage, request):
    assert os.name == "posix", "2C requires Linux signals on ECS"
    root = Path(os.environ["INCIDENT_AGENT_TEST_AUDIT_DIR"]).resolve()
    directory = root / (request.node.name.replace("/", "_") + "-" + uuid4().hex[:8])
    directory.mkdir(parents=True)
    result = Lab(storage, directory)
    with result.connect() as connection:
        connection.execute("CREATE TABLE stage2c_calls (node TEXT NOT NULL)")
    try:
        yield result
    finally:
        result.close()
        with result.connect() as connection:
            result.save("runs", connection.execute("""SELECT run_id,incident_id,thread_id,status,attempt,lease_epoch,
                input_sha256,approval_payload,last_error,created_at,updated_at FROM incident_agent_app.runs""").fetchall())
            result.save("operations", connection.execute("SELECT * FROM incident_agent_app.operations").fetchall())
            result.save("database", {"name": connection.execute("SELECT current_database() AS name").fetchone()["name"]})


@contextmanager
def live_service(lab, *, approved=True):
    clients = create_clients(disable_retries=True)
    name = "stage2c-" + uuid4().hex[:16]
    created = clients.core.create_namespaced_service("agent-demo", client.V1Service(
        metadata=client.V1ObjectMeta(name=name, labels={"incident-agent-acceptance": "2c"}),
        spec=client.V1ServiceSpec(selector={"app": "wrong-service"}, ports=[client.V1ServicePort(port=80)])),
        _request_timeout=REQUEST_TIMEOUT)
    try:
        state = state_with_uid()
        state["request"]["service_name"] = name
        state["remediation_plan"]["parameters"]["resource_name"] = name
        state["evidence"][0]["resource_name"] = name
        state["evidence"][0]["data"].update(name=name, uid=created.metadata.uid)
        state["service_profile"]["profile"]["service_name"] = name
        state["service_profile"]["digest"] = profile_digest(ServiceProfile.model_validate(state["service_profile"]["profile"]))
        state["approval_request"] = build_approval_request(state).model_dump(mode="json")
        state["approval_record"]["approval_id"] = state["approval_request"]["approval_id"]
        lab.state = state
        if approved:
            row = waiting(lab.repo, state)
            lab.repo.queue_approval(row["run_id"], decision_for(state), approval_binding(state))
        else:
            row = lab.repo.accept(incident_id=state["incident_id"], run_id=str(uuid4()), thread_id=str(uuid4()),
                                  payload=state["request"], key=None)
        lab.save("before", service_snapshot(created))
        yield clients, name, row
    finally:
        lab.close()
        try:
            live = clients.core.read_namespaced_service(name, "agent-demo", _request_timeout=REQUEST_TIMEOUT)
            lab.save("after", service_snapshot(live))
            clients.core.delete_namespaced_service(name, "agent-demo",
                body=client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=created.metadata.uid)), _request_timeout=REQUEST_TIMEOUT)
        except ApiException as error:
            if error.status != 404:
                raise


def test_R01_client_disconnect_after_commit_and_concurrent_replay(lab):
    process, address = lab.api(point="after_accept")
    key = "r01-" + uuid4().hex
    payload = {"namespace": "agent-demo", "service_name": "order-service", "description": "R01 acceptance"}
    body = json.dumps(payload).encode()
    port = int(address.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        headers = f"POST /api/v1/incidents HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nIdempotency-Key: {key}\r\nContent-Length: {len(body)}\r\n\r\n"
        connection.sendall(headers.encode() + body)
        lab.barrier(process, "after_accept")
    lab.kill(process)
    committed = lab.repo.by_key(key)
    assert committed and committed["status"] == "queued"
    _, address = lab.api()
    response = httpx.get(address + "/api/v1/incidents/by-idempotency-key/" + key, timeout=10, trust_env=False)
    assert response.status_code == 200 and response.json()["incident_id"] == committed["incident_id"]
    def submit(_):
        result = httpx.post(address + "/api/v1/incidents", json=payload, headers={"Idempotency-Key": key}, timeout=15, trust_env=False)
        assert result.status_code == 202
        return result.json()["incident_id"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert set(pool.map(submit, range(6))) == {committed["incident_id"]}
    with lab.connect() as connection:
        assert connection.execute("SELECT count(*) AS n FROM incident_agent_app.runs").fetchone()["n"] == 1
    fresh_key = "r01-race-" + uuid4().hex
    barrier = threading.Barrier(6)
    def concurrent_create(_):
        barrier.wait(timeout=10)
        response = httpx.post(address + "/api/v1/incidents", json=payload,
                              headers={"Idempotency-Key": fresh_key}, timeout=15, trust_env=False)
        assert response.status_code == 202
        return response.json()["incident_id"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        fresh_ids = set(pool.map(concurrent_create, range(6)))
    assert len(fresh_ids) == 1 and committed["incident_id"] not in fresh_ids
    with lab.connect() as connection:
        assert connection.execute("SELECT count(*) AS n FROM incident_agent_app.runs").fetchone()["n"] == 2
    lab.save("idempotency", {"key": key, "incident_id": committed["incident_id"], "run_id": committed["run_id"]})


@pytest.mark.parametrize("point", ["after_evidence_checkpoint", "model_call"])
@pytest.mark.parametrize("shutdown", ["kill", "term"])
def test_R02_checkpoint_survives_process_stop(lab, point, shutdown):
    row = accept(lab.repo)
    process = lab.start(point=point)
    lab.barrier(process, point)
    if shutdown == "kill":
        lab.kill(process)
        lab.expired(row["run_id"])
    else:
        process.send_signal(signal.SIGTERM)
        lab.release(process, point)
        lab.join(process)
        lab.save("termination", {"signal": "SIGTERM", "returncode": process.returncode})
    lab.join(lab.start())
    result = lab.repo.latest(row["incident_id"])
    assert result["status"] == "succeeded", result["last_error"]
    with lab.connect() as connection:
        counts = {r["node"]: r["n"] for r in connection.execute("SELECT node,count(*) AS n FROM stage2c_calls GROUP BY node").fetchall()}
    assert counts["collect_evidence"] == 1
    assert counts["diagnose_incident"] == (2 if shutdown == "kill" and point == "model_call" else 1)
    assert result["attempt"] == (2 if shutdown == "kill" else 1)
    lab.save("node-counts", counts)


def test_R03_approval_survives_all_app_process_restarts(lab):
    lab.state = state_with_uid()
    row = lab.repo.accept(incident_id=lab.state["incident_id"], run_id=str(uuid4()), thread_id=str(uuid4()),
                          payload=lab.state["request"], key=None)
    lab.join(lab.start(graph="approval"))
    assert lab.repo.latest(row["incident_id"])["status"] == "waiting_approval"
    old, address = lab.api()
    path = "/api/v1/incidents/" + row["incident_id"]
    first = httpx.get(address + path, timeout=10, trust_env=False).json()
    lab.kill(old)
    lab.join(lab.start(graph="approval"))  # Waiting tasks are not claimed.
    _, address = lab.api()
    second = httpx.get(address + path, timeout=10, trust_env=False).json()
    assert first["approval_request"] == second["approval_request"] and second["waiting_for_approval"]
    invalid = {**decision_for(lab.state), "approval_id": "apr-" + "0" * 16}
    assert httpx.post(address + path + "/approval", json=invalid, timeout=10, trust_env=False).status_code == 409
    assert lab.repo.latest(row["incident_id"])["approval_payload"] is None
    response = httpx.post(address + path + "/approval", json=decision_for(lab.state, False), timeout=10, trust_env=False)
    assert response.status_code == 200
    lab.join(lab.start(graph="approval"))
    assert lab.repo.latest(row["incident_id"])["status"] == "succeeded"
    assert not lab.requests()
    lab.save("approval-before", first["approval_request"])
    lab.save("approval-after", second["approval_request"])


@pytest.mark.parametrize("variant,point", [("not_applied", "before_patch"), ("applied", "after_patch_response"),
    ("third_value", "after_patch_response"), ("response_saved", "after_operation_result")])
def test_R04_crash_at_write_boundary_never_blindly_repatches(lab, variant, point):
    with live_service(lab) as (clients, name, row):
        process = lab.start(graph="write", point=point)
        lab.barrier(process, point)
        before = lab.repo.operation(row["run_id"])
        lab.kill(process)
        if variant == "third_value":
            live = clients.core.read_namespaced_service(name, "agent-demo", _request_timeout=REQUEST_TIMEOUT)
            changed = clients.core.patch_namespaced_service(name, "agent-demo", [
                {"op": "test", "path": "/metadata/uid", "value": live.metadata.uid},
                {"op": "test", "path": "/metadata/resourceVersion", "value": live.metadata.resource_version},
                {"op": "replace", "path": "/spec/selector", "value": {"app": "third-party"}}], _request_timeout=REQUEST_TIMEOUT)
            lab.save("external-change", service_snapshot(changed))
        lab.expired(row["run_id"])
        lab.join(lab.start(graph="write"))
        after = lab.repo.operation(row["run_id"])
        assert after["operation_id"] == before["operation_id"]
        confirmed = variant == "response_saved"
        assert after["state"] == ("reconciled" if confirmed else "manual_required")
        assert after["observed_snapshot"] is not None
        assert lab.repo.latest(row["incident_id"])["status"] == ("succeeded" if confirmed else "reconciling")
        assert len(lab.requests()) == (0 if variant == "not_applied" else 1)
        # No public ordinary retry API can bypass the reconciliation obligation.
        _, address = lab.api()
        response = httpx.post(address + "/api/v1/incidents/" + row["incident_id"] + "/runs/" + row["run_id"] + "/retry",
                              json={}, timeout=10, trust_env=False)
        assert response.status_code in {404, 405}
        assert lab.repo.claim("ordinary-retry", 10) is None


def test_R05_competitors_stale_owner_and_duplicate_approval(lab):
    with live_service(lab) as (_, _, row):
        _, address = lab.api()
        path = address + "/api/v1/incidents/" + row["incident_id"] + "/approval"
        # Seed a real pending checkpoint first, so the API can read this run.
        old = lab.start(graph="write", point="after_operation_prepare")
        lab.barrier(old, "after_operation_prepare")
        def repeat(_):
            return httpx.post(path, json=decision_for(lab.state), timeout=10, trust_env=False).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(repeat, range(2))) == [200, 200]
        assert httpx.post(path, json=decision_for(lab.state, False), timeout=10, trust_env=False).status_code == 409
        previous = lab.repo.latest(row["incident_id"])
        old.send_signal(signal.SIGSTOP)  # Stops heartbeat as well as the task.
        lab.expired(row["run_id"])
        new = lab.start(graph="write", point="after_claim")
        lab.barrier(new, "after_claim")
        lab.join(lab.start(graph="write"))  # A competing worker cannot claim it.
        current = lab.repo.latest(row["incident_id"])
        assert current["lease_epoch"] == previous["lease_epoch"] + 1
        old.send_signal(signal.SIGCONT)
        lab.release(old, "after_operation_prepare")
        lab.join(old)
        assert not lab.requests()
        with pytest.raises(LeaseLost):
            lab.repo.finish(previous, "succeeded")
        lab.release(new, "after_claim")
        lab.join(new)
        assert len(lab.requests()) == 1
        assert lab.repo.latest(row["incident_id"])["status"] == "succeeded"
        lab.save("epochs", {"old": previous["lease_epoch"], "new": current["lease_epoch"]})


def test_R05_two_api_processes_compete_for_approval(lab):
    with live_service(lab, approved=False) as (_, _, row):
        lab.join(lab.start(graph="approval"))
        assert lab.repo.latest(row["incident_id"])["status"] == "waiting_approval"
        _, first = lab.api()
        _, second = lab.api()
        barrier = threading.Barrier(2)
        def submit(pair):
            address, approved = pair
            barrier.wait(timeout=10)
            response = httpx.post(address + "/api/v1/incidents/" + row["incident_id"] + "/approval",
                                  json=decision_for(lab.state, approved), timeout=15, trust_env=False)
            return approved, response.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, [(first, True), (second, False)]))
        assert sorted(code for _, code in results) == [200, 409]
        winner = next(approved for approved, code in results if code == 200)
        assert lab.repo.latest(row["incident_id"])["approval_payload"]["decision"]["approved"] is winner
        lab.join(lab.start(graph="approval"))
        assert lab.repo.latest(row["incident_id"])["status"] == "succeeded"
        assert len(lab.requests()) == int(winner)
        lab.save("approval-race", {"results": results, "winner": winner})


@pytest.mark.parametrize("point,patches", [("before_operation_read", 0), ("before_operation_prepare", 0),
                                         ("before_operation_record", 1)])
def test_R06_storage_failures_at_three_boundaries(lab, point, patches):
    with live_service(lab) as (_, _, row):
        process = lab.start(graph="write", point=point, mode="storage_error")
        lab.barrier(process, point)
        lab.join(process)
        assert len(lab.requests()) == patches
        result = lab.repo.latest(row["incident_id"])
        if patches:
            assert result["status"] == "reconciling"
            assert lab.repo.operation(row["run_id"])["state"] == "manual_required"
            lab.join(lab.start(graph="write"))
            assert len(lab.requests()) == 1
        else:
            assert lab.repo.operation(row["run_id"]) is None
            assert result["status"] in {"failed", "retry_scheduled"}
            if result["status"] == "retry_scheduled":
                def due():
                    with lab.connect() as connection:
                        return connection.execute("SELECT next_retry_at<=clock_timestamp() AS due FROM incident_agent_app.runs WHERE run_id=%s",
                                                  (row["run_id"],)).fetchone()["due"]
                wait_until(due, timeout=20)
                lab.join(lab.start(graph="write"))
                assert lab.repo.latest(row["incident_id"])["status"] == "succeeded"
                assert len(lab.requests()) == 1
            else:
                lab.join(lab.start(graph="write"))
                assert not lab.requests()  # A saved business-failure END is not restarted.
        lab.save("failure-boundary", {"point": point, "agent_patch_requests": patches, "status": result["status"]})
