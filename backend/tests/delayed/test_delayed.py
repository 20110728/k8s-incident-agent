from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
import subprocess
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from backend.app.agent.observation import identity, POLICY
from backend.app.persistence.controls import ControlRepository
from backend.app.persistence.delayed_rechecks import activate_due, history
from backend.app.persistence.observations import ObservationStore
from backend.app.persistence.leases import LeaseLost
from backend.app.persistence.rounds import RoundConflict
from backend.app.runtime.delayed_rechecks import execute_delayed_recheck
from backend.app.runtime.interactions import execute_interaction
from backend.app.services.interaction_schemas import CreateInteraction
from backend.app.services.recheck_service import IncidentRecheckService, RecheckRequest
from backend.tests.runtime.test_worker_postgres import storage
from backend.tests.messages.test_messages import legacy
from backend.tests.diagnosis_policy.test_stage4 import state
from backend.tests.business_recovery.test_post_repair import bundle_from_state
from backend.tests.interactions.test_interactions import reference, client_for


def prepare(storage, state):
    repo = ControlRepository(storage[0])
    incident = legacy(storage[0])
    state = deepcopy(state)
    state.update(incident_id=incident, phase="remediation_skipped")
    bundle = bundle_from_state(state)
    bundle["service"]["uid"] = "service-uid"
    result = IncidentRecheckService(SimpleNamespace(get_incident=lambda _: SimpleNamespace(state=state, waiting_for_approval=False)),
        SimpleNamespace(collect=lambda *_: deepcopy(bundle)), SimpleNamespace(append=lambda _: None)).create(incident, RecheckRequest()).model_dump(mode="json")
    assert result["status"] == "passed"
    payload = {"policy": POLICY, "status": "passed", "target": identity(result["service_profile"], result["evidence"]),
               "started_at": result["started_at"], "finished_at": result["finished_at"], "consecutive": 3,
               "samples": [], "last_result": result}
    store = ObservationStore(repo._connect, "test:" + incident, incident)
    store.open(payload)
    store.save(payload)
    return repo, incident, state, bundle, store, payload


def due(repo, incident):
    with repo._connect() as conn:
        conn.execute("""UPDATE incident_agent_app.delayed_rechecks SET due_at=clock_timestamp()-interval '1 second',
            next_attempt_at=clock_timestamp()-interval '1 second' WHERE incident_id=%s""", (incident,))


def execute(repo, bundle, **kwargs):
    activate_due(repo)
    lease = repo.claim("delayed-test", 30)
    assert lease is not None
    execute_delayed_recheck(repo, lease, Event(), collector_factory=lambda: SimpleNamespace(collect=lambda *_: deepcopy(bundle)), **kwargs)
    return lease


def test_schedule_once_and_pending_does_not_occupy_lane(storage, state):
    repo, incident, state, _, store, payload = prepare(storage, state)
    first = history(repo, incident)["items"][0]
    for _ in range(3):
        store.save(payload)
    activate_due(repo)
    assert len(history(repo, incident)["items"]) == 1
    assert history(repo, incident)["items"][0]["due_at"] == first["due_at"]
    assert repo._read("SELECT run_id FROM incident_agent_app.runs", ()) == []
    # Pending follow-up permits normal user interaction immediately.
    repo.accept_interaction(incident, CreateInteraction(intent="explain", client_message_id="explain", content="explain"), None, [reference(state)])
    due(repo, incident)
    activate_due(repo)
    item = history(repo, incident)["items"][0]
    assert item["status"] == "pending" and item["reason"] == "EVENT_BUSY"
    lease = repo.claim("explain", 30)
    repo.complete(lease, {"intent": "explain", "answer": "historical"})
    due(repo, incident)
    activate_due(repo)
    assert history(repo, incident)["items"][0]["status"] == "running"


def test_concurrent_activation_and_history_read_are_idempotent(storage, state):
    repo, incident, state, bundle, _, _ = prepare(storage, state)
    due(repo, incident)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: activate_due(repo), range(8)))
    assert len(repo._read("SELECT run_id FROM incident_agent_app.runs", ())) == 1
    with pytest.raises(RoundConflict):
        repo.accept_round(incident, "not-needed", "new-investigation", None, state)
    execute(repo, bundle)
    item = history(repo, incident)["items"][0]
    assert item["status"] == "passed" and item["initial_result"]["status"] == "passed"
    assert item["result"]["started_at"] and item["result"]["finished_at"]
    with client_for(repo) as client:
        url = f"/api/v1/incidents/{incident}/delayed-rechecks"
        assert client.get(url).status_code == 200
        assert client.get(url, params={"before_sequence": item["sequence"]}).json()["items"] == []
        assert client.get(url, params={"before_sequence": 0}).status_code == 422
    assert repo._read("SELECT operation_id FROM incident_agent_app.operations", ()) == []


@pytest.mark.parametrize("mode,expected", [("failure", "relapsed"), ("unknown", "unknown"), ("uid", "invalidated"), ("profile", "invalidated")])
def test_delayed_outcome_does_not_rewrite_initial_pass(storage, state, mode, expected):
    repo, incident, _, bundle, _, payload = prepare(storage, state)
    if mode == "failure":
        bundle["business_checks"][0].update(status="failed", error_code="HTTP_STATUS_MISMATCH", content_matches=False)
    elif mode == "unknown":
        bundle["business_checks"] = []
    elif mode == "uid":
        bundle["service"]["uid"] = "replacement-service"
    else:
        bundle["service_profile"].update(status="unavailable", digest="changed-contract")
    due(repo, incident)
    execute(repo, bundle)
    item = history(repo, incident)["items"][0]
    assert item["status"] == expected
    assert item["initial_result"]["status"] == "passed"
    with repo._connect() as conn:
        saved = conn.execute("SELECT payload FROM incident_agent_app.observation_windows").fetchone()["payload"]
        assert saved == payload
        assert conn.execute("SELECT phase FROM incident_agent_app.incidents WHERE incident_id=%s", (incident,)).fetchone()["phase"] == "remediation_skipped"
        assert conn.execute("SELECT count(*) AS n FROM incident_agent_app.operations").fetchone()["n"] == 0


@pytest.mark.parametrize("mode,expected", [("revision", "invalidated"), ("expiry", "expired")])
def test_changed_basis_and_overdue_tasks_do_not_start(storage, state, mode, expected):
    repo, incident, _, _, _, _ = prepare(storage, state)
    due(repo, incident)
    with repo._connect() as conn:
        if mode == "revision":
            conn.execute("UPDATE incident_agent_app.incidents SET event_revision=event_revision+1 WHERE incident_id=%s", (incident,))
        else:
            conn.execute("UPDATE incident_agent_app.delayed_rechecks SET expires_at=clock_timestamp()-interval '1 second'")
    activate_due(repo)
    assert history(repo, incident)["items"][0]["status"] == expected
    assert repo._read("SELECT run_id FROM incident_agent_app.runs", ()) == []


def test_expiry_during_read_keeps_actual_times_but_never_passes(storage, state):
    repo, incident, _, bundle, _, _ = prepare(storage, state)
    due(repo, incident)
    activate_due(repo)
    lease = repo.claim("late", 30)
    def collect(*_):
        with repo._connect() as conn:
            conn.execute("UPDATE incident_agent_app.delayed_rechecks SET expires_at=clock_timestamp()-interval '1 second'")
        return deepcopy(bundle)
    execute_delayed_recheck(repo, lease, Event(), collector_factory=lambda: SimpleNamespace(collect=collect))
    item = history(repo, incident)["items"][0]
    assert item["status"] == "expired" and item["result"]["finished_at"]


def test_new_process_activates_persisted_pending_task(storage, state):
    repo, incident, _, bundle, _, _ = prepare(storage, state)
    due(repo, incident)
    environment = dict(os.environ, DELAYED_TEST_DSN=storage[2].database_url.get_secret_value())
    code = """import os
from functools import partial
from backend.app.persistence.database import connect_database
from backend.app.persistence.settings import DatabaseSettings
from backend.app.persistence.controls import ControlRepository
repo=ControlRepository(partial(connect_database,DatabaseSettings(database_url=os.environ['DELAYED_TEST_DSN'])))
repo.activate_delayed()
"""
    subprocess.run([sys.executable, "-c", code], env=environment, check=True, timeout=30)
    assert history(repo, incident)["items"][0]["status"] == "running"
    execute(repo, bundle)
    assert history(repo, incident)["items"][0]["status"] == "passed"


def test_terminal_result_survives_reclaim_without_another_physical_sample(storage, state):
    repo, incident, _, bundle, _, _ = prepare(storage, state)
    due(repo, incident)
    activate_due(repo)
    lease = repo.claim("first", 30)
    def crash(*args, **kwargs):
        raise SystemExit("after result commit")
    with pytest.raises(SystemExit):
        execute_delayed_recheck(repo, lease, Event(), collector_factory=lambda: SimpleNamespace(collect=lambda *_: deepcopy(bundle)), complete=crash)
    with repo._connect() as conn:
        conn.execute("UPDATE incident_agent_app.runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE run_id=%s", (lease["run_id"],))
    next_lease = repo.claim("second", 30)
    execute_delayed_recheck(repo, next_lease, Event(), collector_factory=lambda: pytest.fail("terminal result resampled"))
    with pytest.raises(LeaseLost):
        execute_delayed_recheck(repo, lease, Event())
    assert history(repo, incident)["items"][0]["status"] == "passed"


def test_single_recheck_does_not_schedule_a_followup(storage, state):
    repo = ControlRepository(storage[0])
    incident = legacy(storage[0])
    state.update(incident_id=incident, phase="remediation_skipped")
    repo.accept_interaction(incident, CreateInteraction(intent="recheck", client_message_id="single", content="check"), None, [reference(state)])
    lease = repo.claim("single", 30)
    execute_interaction(repo, lease, Event(), collector_factory=lambda: SimpleNamespace(collect=lambda *_: bundle_from_state(state)))
    assert history(repo, incident)["items"] == []


def test_interrupted_read_can_be_repeated_but_publishes_only_one_record(storage, state):
    repo, incident, _, bundle, _, _ = prepare(storage, state)
    due(repo, incident)
    activate_due(repo)
    lease = repo.claim("crashed-reader", 30)
    calls = []
    def interrupted(*_):
        calls.append("interrupted")
        raise SystemExit("read outcome not stored")
    with pytest.raises(SystemExit):
        execute_delayed_recheck(repo, lease, Event(), collector_factory=lambda: SimpleNamespace(collect=interrupted))
    with repo._connect() as conn:
        conn.execute("UPDATE incident_agent_app.runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE run_id=%s", (lease["run_id"],))
    recovered = repo.claim("replacement-reader", 30)
    def collect(*_):
        calls.append("repeated")
        return deepcopy(bundle)
    execute_delayed_recheck(repo, recovered, Event(), collector_factory=lambda: SimpleNamespace(collect=collect))
    assert calls == ["interrupted", "repeated"]
    assert len(history(repo, incident)["items"]) == 1
    assert history(repo, incident)["items"][0]["status"] == "passed"


def test_new_information_during_sampling_invalidates_the_delayed_result(storage, state):
    repo, incident, _, bundle, _, _ = prepare(storage, state)
    due(repo, incident)
    activate_due(repo)
    lease = repo.claim("before-change", 30)
    def collect(*_):
        with repo._connect() as conn:
            conn.execute("UPDATE incident_agent_app.incidents SET event_revision=event_revision+1 WHERE incident_id=%s", (incident,))
        return deepcopy(bundle)
    execute_delayed_recheck(repo, lease, Event(), collector_factory=lambda: SimpleNamespace(collect=collect))
    item = history(repo, incident)["items"][0]
    assert item["status"] == "invalidated" and item["result"]["finished_at"]
