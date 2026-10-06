"""2C subprocess entry point. Synthetic investigation; real DB and Service PATCH."""
import argparse
from contextlib import contextmanager
from functools import partial
import json
import os
from pathlib import Path
import signal
import socket
from unittest.mock import patch

from backend.app.persistence import serialization_security
from backend.app.agent.graph import route_after_approval, route_after_execution
from backend.app.agent.nodes import make_execute_remediation_node, request_human_approval
from backend.app.agent.state import IncidentState
from backend.app.persistence.database import connect_database
from backend.app.persistence.operations import OperationRepository
from backend.app.persistence.settings import get_database_settings
from backend.app.runtime.checkpointer import fenced_checkpointer
from backend.app.runtime.failpoints import get_failpoints, hit
from backend.app.runtime.operations import LedgerExecutor
from backend.app.runtime.settings import WorkerSettings
from backend.app.runtime.telemetry import report
from backend.app.runtime.worker import Worker
from backend.app.tools.client import create_clients
from langgraph.graph import StateGraph, START, END
from psycopg.conninfo import conninfo_to_dict
from backend.app.persistence.database import normalize_psycopg_dsn


def append(directory, name, value):
    with (directory / name).open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, default=str) + "\n")
        output.flush()
        os.fsync(output.fileno())


def service_snapshot(resource):
    return {"uid": resource.metadata.uid, "resource_version": resource.metadata.resource_version,
            "generation": resource.metadata.generation, "selector": resource.spec.selector}


class Transport:
    def __init__(self, core, directory):
        self.core, self.directory = core, directory

    def read_namespaced_service(self, **kwargs):
        return self.core.read_namespaced_service(**kwargs)

    def patch_namespaced_service(self, **kwargs):
        append(self.directory, "api.jsonl", {"event": "request", "patch": kwargs["body"]})
        result = self.core.patch_namespaced_service(**kwargs)
        append(self.directory, "api.jsonl", {"event": "response", "snapshot": service_snapshot(result)})
        return result


class SynchronousGraph:
    def __init__(self, graph):
        self.graph = graph
    def get_state(self, config):
        return self.graph.get_state(config)
    def invoke(self, value, config):
        # Test barriers after a checkpoint must precede the next task, even
        # when the installed LangGraph default persists checkpoints async.
        return self.graph.invoke(value, config=config, durability="sync")


def context_factory(settings, repo, directory, kind):
    @contextmanager
    def context(lease, lost):
        with fenced_checkpointer(settings, repo, lease, lost) as saver:
            builder = StateGraph(IncidentState)
            if kind == "readonly":
                def collect(state):
                    with repo._connect() as connection:
                        connection.execute("INSERT INTO stage2c_calls (node) VALUES ('collect_evidence')")
                    report("test_node_enter", lease, node="collect_evidence")
                    return {"phase": "evidence_collected"}
                def diagnose(state):
                    with repo._connect() as connection:
                        connection.execute("INSERT INTO stage2c_calls (node) VALUES ('diagnose_incident')")
                    report("test_node_enter", lease, node="diagnose_incident")
                    hit("model_call", lease)
                    return {"phase": "remediation_skipped"}
                builder.add_node("collect_evidence", collect)
                builder.add_node("diagnose_incident", diagnose)
                builder.add_edge(START, "collect_evidence")
                builder.add_edge("collect_evidence", "diagnose_incident")
                builder.add_edge("diagnose_incident", END)
            else:
                seed = json.loads((directory / "state.json").read_text(encoding="utf-8"))
                if kind == "approval":
                    seed.update(phase="awaiting_approval", approval_status="pending", approved=None, approval_record=None)
                builder.add_node("prepare_approval", lambda state: seed)
                builder.add_edge(START, "prepare_approval")
                # No Kubernetes client is initialized for a rejected approval.
                def execute(state):
                    clients = create_clients(disable_retries=True)
                    from types import SimpleNamespace
                    wrapped = SimpleNamespace(core=Transport(clients.core, directory), apps=clients.apps)
                    return make_execute_remediation_node(LedgerExecutor(wrapped, repo, lease, lost))(state)
                builder.add_node("execute_remediation", execute)
                builder.add_node("verify_recovery", lambda state: {"phase": "verification_succeeded"})
                if kind == "approval":
                    builder.add_node("request_human_approval", request_human_approval)
                    builder.add_edge("prepare_approval", "request_human_approval")
                    builder.add_conditional_edges("request_human_approval", route_after_approval,
                                                  {"execute": "execute_remediation", "stop": END})
                else:
                    builder.add_edge("prepare_approval", "execute_remediation")
                builder.add_conditional_edges("execute_remediation", route_after_execution,
                                              {"verify": "verify_recovery", "stop": END})
                builder.add_edge("verify_recovery", END)
            yield SynchronousGraph(builder.compile(checkpointer=saver))
    return context


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("api", "worker"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--graph", choices=("readonly", "approval", "write"), default="readonly")
    args = parser.parse_args()
    settings = get_database_settings()
    name = conninfo_to_dict(normalize_psycopg_dsn(settings.database_url.get_secret_value()))["dbname"]
    if os.environ.get("INCIDENT_AGENT_API_ENVIRONMENT") != "test" or not name.startswith("incident_agent_test_2c_"):
        raise SystemExit("2C driver requires an isolated 2C test database")
    get_failpoints()
    if args.action == "api":
        import uvicorn
        from backend.app.main import create_app
        from backend.app.config import ApiSettings
        from backend.app.api.dependencies import incident_service_context
        app = create_app(ApiSettings(_env_file=None, environment="test", execution_mode="queued"),
                         service_context_factory=partial(incident_service_context, execution_mode="queued"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            port_file = args.directory / "api-port.tmp"
            port_file.write_text(json.dumps({"port": sock.getsockname()[1]}), encoding="utf-8")
            port_file.replace(args.directory / "api-port.json")
            uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False)).run(sockets=[sock])
    else:
        repo = OperationRepository(partial(connect_database, settings))
        worker = Worker(repo, context_factory(settings, repo, args.directory, args.graph),
                        WorkerSettings(_env_file=None, lease_seconds=2, heartbeat_seconds=0.2, shutdown_seconds=5))
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: worker.stop.set())
        # Only the release evidence is synthetic. Authorization, journal and
        # Kubernetes UID/resourceVersion conditions use the production code.
        with patch("backend.app.runtime.operations.revalidate_live_profile", return_value={}):
            worker.run(once=True)
        os._exit(0)


if __name__ == "__main__":
    main()
