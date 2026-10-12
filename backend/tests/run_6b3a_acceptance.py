"""6B-3a public investigation view; no live model calls."""
from backend.tests.run_6b1_acceptance import main


if __name__ == "__main__":
    raise SystemExit(main(stage="6b3a", extra_targets=(
        "backend/tests/investigation_production", "backend/tests/investigation_dialogue",
        "backend/tests/investigation_presentation", "backend/tests/dialogue", "backend/tests/rounds",
        "backend/tests/api"), required_reports=("evidence-change.json", "human-resampling.json", "production-flow.json")))
