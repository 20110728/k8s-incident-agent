"""One block: production routing and approval integration on isolated PostgreSQL."""
from backend.tests.run_6b1_acceptance import main


if __name__ == "__main__":
    raise SystemExit(main(stage="6b2b", extra_targets=(
        "backend/tests/investigation_production", "backend/tests/investigation_dialogue",
        "backend/tests/dialogue", "backend/tests/rounds", "backend/tests/runtime/test_worker_postgres.py",
        "backend/tests/persistence/test_run_contracts.py"),
        required_reports=("evidence-change.json", "human-resampling.json", "production-flow.json")))
