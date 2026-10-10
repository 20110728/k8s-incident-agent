"""C2 investigation context, production handoff and human-resume regression."""
from backend.tests.run_6b1_acceptance import main


if __name__ == "__main__":
    raise SystemExit(main(stage="6bc2", base_targets=(
        "backend/tests/investigation_loop", "backend/tests/investigation_dialogue",
        "backend/tests/investigation_production",
        "backend/tests/investigation/test_tools.py", "backend/tests/unit/test_service_and_pod_tools.py",
    ), required_reports=("working-context.json", "c2-fix.json", "c2-robustness.json", "diagnosis-contract.json", "safe-conclusion.json", "raw-evidence.json", "evidence-change.json", "human-resampling.json", "production-flow.json")))
