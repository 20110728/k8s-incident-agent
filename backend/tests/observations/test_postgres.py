from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import pytest

from backend.app.agent.stability import observe_recheck, WindowRecoveryVerifier
from backend.app.persistence.observations import ObservationStore
from backend.app.persistence.leases import LeaseLost
from backend.app.runtime.interactions import execute_interaction
from backend.app.services.interaction_schemas import CreateInteraction
from backend.tests.runtime.test_worker_postgres import storage, accept
from backend.tests.interactions.test_interactions import seed, reference, client_for
from backend.tests.diagnosis_policy.test_stage4 import state
from backend.tests.business_recovery.test_post_repair import bundle_from_state, setup
from backend.tests.observations.test_window import Clock, window, observation


def test_durable_samples_reclaim_and_fencing(storage):
    connect, repo, _ = storage
    task = accept(repo)
    lease = repo.claim("first", 30)
    store = ObservationStore(connect, "test:" + task["run_id"], task["incident_id"], repo=repo, lease=lease)
    clock = Clock()
    count = 0
    def crash(_):
        nonlocal count
        count += 1
        if count == 3:
            raise SystemExit()
        return observation()
    with pytest.raises(SystemExit):
        window(store, clock).run(crash)
    with connect() as conn:
        conn.execute("UPDATE incident_agent_app.runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE run_id=%s", (task["run_id"],))
    next_lease = repo.claim("second", 30)
    recovered = ObservationStore(connect, store.key, task["incident_id"], repo=repo, lease=next_lease)
    result = window(recovered, clock).run(lambda _: observation())
    assert result["status"] == "passed" and len(result["samples"]) == 6
    assert result["deadline"] == 1120 and result["samples"][2]["status"] == "interrupted"
    with pytest.raises(LeaseLost):
        store.save(result)
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM incident_agent_app.observation_windows").fetchone()["n"] == 1


def test_explicit_observation_worker_idempotency_and_original_unchanged(storage, state, monkeypatch):
    import backend.app.agent.stability as module
    repo, incident, _ = seed(storage)
    state.update(incident_id=incident, phase="remediation_skipped")
    bundle = bundle_from_state(state)
    bundle["service"]["uid"] = "service-uid"
    original = deepcopy(state)
    refs = [reference(state)]
    body = CreateInteraction(client_message_id="stable-window", intent="observe", content="Observe stability")
    with client_for(repo, refs) as client:
        url = f"/api/v1/incidents/{incident}/interactions"
        first = client.post(url, json=body.model_dump())
        assert first.status_code == 202, first.text
        assert client.post(url, json=body.model_dump()).json()["run"]["run_id"] == first.json()["run"]["run_id"]
    clock, calls = Clock(), []
    def fast_observe(*args):
        return observe_recheck(*args, window_factory=lambda store: window(store, clock))
    monkeypatch.setattr(module, "observe_recheck", fast_observe)
    collector = SimpleNamespace(collect=lambda *args: (calls.append(args), deepcopy(bundle))[1])
    lease = repo.claim("observer", 30)
    execute_interaction(repo, lease, Event(), collector_factory=lambda: collector,
                        model_factory=lambda: pytest.fail("observation called model"))
    saved = repo.saved_recheck(lease)
    assert saved["status"] == "passed" and len(calls) == 3
    assert saved["observation"]["policy"]["version"] == "recovery-window-v1"
    assert state == original
    with repo._connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM incident_agent_app.rechecks").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM incident_agent_app.operations").fetchone()["n"] == 0


def test_automatic_verification_uses_window_and_old_result_schema_stays_compatible(storage, setup):
    connect, repo, _ = storage
    task = accept(repo)
    lease = repo.claim("repair-verifier", 30)
    state, bundle, resource, collector, single = setup
    state["incident_id"] = task["incident_id"]
    state["action_result"]["execution_id"] = resource.execution_id
    bundle["service"]["uid"] = "service-uid"
    clock = Clock()
    verifier = WindowRecoveryVerifier(single, repository=repo, lease=lease,
                                      window_factory=lambda store: window(store, clock))
    result = verifier.verify(state)
    assert result.status == "succeeded" and result.observation["consecutive"] == 3
    assert collector.collect.call_count == 3
    assert verifier.verify(state).status == "succeeded"
    assert collector.collect.call_count == 3
    assert resource.observation is None


def test_migration_eleven_is_idempotent(storage):
    from backend.app.persistence.migrations import run_migrations
    with storage[0]() as conn:
        assert run_migrations(conn) == []
        assert conn.execute("SELECT name FROM incident_agent_app.schema_migrations WHERE version=11").fetchone()["name"] == "observation_windows"
