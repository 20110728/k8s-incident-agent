"""6B-2a: one block, isolated DB, controlled providers, no production switch."""
from backend.tests.run_6b1_acceptance import main


if __name__ == "__main__":
    raise SystemExit(main(stage="6b2a", extra_targets=("backend/tests/investigation_dialogue", "backend/tests/dialogue"),
                          required_reports=("evidence-change.json", "human-resampling.json")))
