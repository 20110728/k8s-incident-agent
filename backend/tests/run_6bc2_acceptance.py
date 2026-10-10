"""C2 investigation context, production handoff and human-resume regression."""
from backend.tests.run_6b1_acceptance import main


if __name__ == "__main__":
    raise SystemExit(main(stage="6bc2", base_targets=(
        "backend/tests/investigation_loop", "backend/tests/investigation_dialogue",
        "backend/tests/investigation_production",
    ), required_reports=("working-context.json", "evidence-change.json", "human-resampling.json", "production-flow.json")))
