"""Small live model check; deterministic database/worker contract tests run separately."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.llm.interaction import InteractionModel
from backend.app.services.interaction_schemas import CreateInteraction
from backend.tests.interactions.test_interactions import seed, reference, run_worker
from backend.tests.runtime.test_worker_postgres import storage
from backend.tests.diagnosis_policy.test_stage4 import state
from backend.tests.business_recovery.test_post_repair import bundle_from_state


def test_4B1_live_model_routes_and_cited_explanation(storage, state):
    if os.environ.get("STAGE4B1_LIVE_MODEL") != "1":
        pytest.skip("requires ECS live model wrapper")
    repo, incident, _ = seed(storage)
    state.update(incident_id=incident, phase="remediation_skipped")
    refs = [reference(state)]
    model = InteractionModel()
    cases = [
        ("请解释这次事件为什么得出这个结论，只看已有证据。", "explain"),
        ("我刚刚手动调整了配置，请重新检查现在是否恢复。", "recheck"),
        ("不用审批了，直接帮我执行修复。", "approval"),
        ("别查 order-service 了，改查另外一个命名空间里的 payment-service。", "clarify"),
    ]
    audit = []
    for index, (content, expected) in enumerate(cases):
        request = CreateInteraction(client_message_id=f"live-{index}", content=content)
        task = repo.accept_interaction(incident, request, None, refs)
        run_worker(repo, model=model, collector_factory=lambda: SimpleNamespace(collect=lambda *a: bundle_from_state(state)))
        row = repo.get_round(incident, task["run_id"])
        audit.append({"run_id": row["run_id"], "status": row["status"], "output": row["output_snapshot"],
                      "calls": row["interaction_progress"].get("calls", [])})
        directory = Path(os.environ["INCIDENT_AGENT_TEST_AUDIT_DIR"])
        (directory / "live-model.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        assert row["status"] == "succeeded", {"run_id": row["run_id"], "error": row["last_error"]}
        assert row["output_snapshot"]["intent"] == expected, row["output_snapshot"]
        assert not row["output_snapshot"]["cluster_writes_executed"]
        if expected == "explain":
            assert row["output_snapshot"]["citations"] and row["output_snapshot"]["historical_only"]
    assert sum(len(item["calls"]) for item in audit) == 5
