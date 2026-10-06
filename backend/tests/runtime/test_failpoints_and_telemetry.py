import json
from dataclasses import dataclass

import pytest
from psycopg import OperationalError

from backend.app.runtime.failpoints import FailureSettings, Failpoints
from backend.app.runtime.telemetry import report, digest
from backend.tests.runtime.test_worker_contracts import lease


@pytest.mark.parametrize("environment,name", [("production", "incident_agent_test_2c_a"),
    ("development", "incident_agent_test_2c_a"), ("test", "incident_agent"), ("test", None)])
def test_failpoints_cannot_target_production_or_application_database(tmp_path, environment, name):
    settings = FailureSettings(_env_file=None, environment=environment, failpoint="after_claim", barrier_dir=tmp_path)
    with pytest.raises(ValueError, match="isolated test database"):
        Failpoints(settings, database_name=name)
    assert list(tmp_path.iterdir()) == []


def test_disabled_failpoints_do_not_need_database_or_create_files():
    disabled = Failpoints(FailureSettings(_env_file=None, failpoint=None))
    disabled.hit("after_patch_response")


def test_production_api_rejects_even_a_valid_test_failpoint(monkeypatch, tmp_path):
    from backend.app import main
    from backend.app.config import ApiSettings
    failure = Failpoints(FailureSettings(_env_file=None, environment="test", failpoint="after_claim",
                         barrier_dir=tmp_path), database_name="incident_agent_test_2c_a")
    monkeypatch.setattr(main, "get_failpoints", lambda: failure)
    with pytest.raises(ValueError, match="forbidden"):
        main.create_app(ApiSettings(_env_file=None, environment="production"))


def test_storage_failure_is_one_shot_and_retains_barrier(tmp_path):
    point = "before_operation_record"
    failure = Failpoints(FailureSettings(_env_file=None, environment="test", failpoint=point,
                         failpoint_mode="storage_error", barrier_dir=tmp_path), database_name="incident_agent_test_2c_a")
    with pytest.raises(OperationalError):
        failure.hit(point, lease())
    assert (tmp_path / (point + ".reached")).exists()
    failure.hit(point, lease())  # Storage recovers; reconciliation can commit.


def test_telemetry_correlates_without_leaking_payload(capsys):
    secret = {"api_key": "sk-do-not-log", "dsn": "postgres://user:password@db/private", "prompt": "private prompt"}
    report("dependency_completed", lease(), node="diagnose", operation_id="op-test",
           input_value=secret, output_value={"answer": "private diagnosis"}, elapsed_ms=12)
    captured = capsys.readouterr()
    assert captured.out == ""  # Diagnostic events must not pollute JSON results.
    output = captured.err
    assert all(value not in output for value in [*secret.values(), "private diagnosis", "password"])
    row = json.loads(output)
    assert (row["incident_id"], row["run_id"], row["thread_id"], row["epoch"], row["attempt"]) == ("incident", "run", "thread", 1, 1)
    assert len(row["input_sha256"]) == len(row["output_sha256"]) == 64
    assert row["operation_id"] == "op-test" and row["timestamp"] and row["elapsed_ms"] == 12


def test_dataclass_output_digest_depends_on_output():
    @dataclass
    class Output:
        answer: str
    assert digest(Output("first")) != digest(Output("second"))


def test_sink_failure_does_not_change_committed_operation(monkeypatch):
    def broken(*args, **kwargs):
        raise BrokenPipeError()
    monkeypatch.setattr("builtins.print", broken)
    report("operation_result", lease())
