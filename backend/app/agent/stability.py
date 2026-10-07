"""Wire existing conservative single-sample judgments into a finite window."""
from functools import partial
from types import SimpleNamespace
from uuid import uuid4

from backend.app.agent.observation import ObservationWindow, identity, summary
from backend.app.agent.schemas import RecoveryVerificationResult
from backend.app.persistence.database import connect_database
from backend.app.persistence.observations import ObservationStore
from backend.app.persistence.settings import get_database_settings
from backend.app.services.recheck_service import IncidentRecheckService, RecheckRequest, RecheckResult
from backend.app.tools.deadline import read_budget


class WindowRecoveryVerifier:
    def __init__(self, single, *, repository=None, lease=None, window_factory=ObservationWindow):
        self.single, self.repository, self.lease = single, repository, lease
        self.window_factory = window_factory

    def verify(self, state):
        action = state.get("action_result") or {}
        if hasattr(action, "model_dump"):
            action = action.model_dump(mode="json")
        if action.get("status") != "succeeded":
            return self.single.verify(state)
        repo, lease = self.repository, self.lease
        connect = repo._connect if repo else partial(connect_database, get_database_settings())
        key = "repair:" + (lease["run_id"] if lease else state["incident_id"] + ":" + action["execution_id"])
        store = ObservationStore(connect, key, state["incident_id"], repo=repo, lease=lease)

        def sample(seconds):
            with read_budget(seconds):
                result = self.single.verify(state).model_dump(mode="json")
            return {"status": "passed" if result["status"] == "succeeded" else "unknown" if result["business_status"] in {"unknown", "skipped"} else "failed",
                    "resource_status": result["resource_status"], "business_status": result["business_status"],
                    "target": identity(result.get("post_repair_profile") or {}, result["post_repair_evidence"]),
                    "invalidated": "RECOVERY_TARGET_CHANGED_OR_UNBOUND" in (result.get("error_message") or ""),
                    "error_code": result.get("error_code"), "result": result}

        window = self.window_factory(store).run(sample)
        result = window["last_result"]
        if result is None:
            result = {"execution_id": action["execution_id"], "action": action["action"],
                      "attempts": 0, "status": "timeout", "started_at": window["started_at"],
                      "finished_at": window.get("finished_at", window["started_at"]), "message": "Observation deadline exhausted."}
        passed = window["status"] == "passed"
        result.update(observation=summary(window), status="succeeded" if passed else "failed",
                      started_at=window["started_at"], finished_at=window.get("finished_at") or window["samples"][-1]["finished_at"],
                      attempts=len(window["samples"]), error_code=None if passed else "STABILITY_" + window["status"].upper(),
                      error_message=None if passed else window["reason"],
                      message="连续观察通过，仅限登记资源和业务采样范围。" if passed else "尚未确认稳定恢复，请查看连续采样记录。")
        return RecoveryVerificationResult.model_validate(result)


def observe_recheck(repo, lease, collector, state, note, *, window_factory=ObservationWindow):
    store = ObservationStore(repo._connect, "interaction:" + lease["run_id"], lease["incident_id"], repo=repo, lease=lease)
    frozen = SimpleNamespace(state=state, waiting_for_approval=False)
    service = IncidentRecheckService(SimpleNamespace(get_incident=lambda _: frozen), collector,
                                    SimpleNamespace(append=lambda _: None))

    def sample(seconds):
        with read_budget(seconds):
            result = service.create(lease["incident_id"], RecheckRequest(note=note)).model_dump(mode="json")
        return {"status": result["status"], "resource_status": result["resource_status"],
                "business_status": result["business_status"], "error_code": result["error_code"],
                "target": identity(result["service_profile"], result["evidence"]), "result": result}

    window = window_factory(store).run(sample)
    result = window["last_result"] or dict(recheck_id=str(uuid4()), incident_id=lease["incident_id"],
        started_at=window["started_at"], finished_at=window["started_at"], note=note, status="unknown",
        resource_status="unknown", business_status="unknown", target_comparison={"status": "unknown"},
        evidence=[], service_profile={}, policy_facts={}, collection_errors=[], unverified_scope=["Observation was interrupted"],
        excluded_terminating_pods=[])
    result.update(status="passed" if window["status"] == "passed" else "unknown",
                  started_at=window["started_at"], finished_at=window.get("finished_at") or window["samples"][-1]["finished_at"],
                  observation=summary(window), error_code=None if window["status"] == "passed" else "STABILITY_" + window["status"].upper())
    saved = RecheckResult.model_validate(result)
    repo.save_recheck(lease, saved)
    return repo.saved_recheck(lease)
