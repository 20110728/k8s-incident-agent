"""C1 focused program-extraction/JSONB/export acceptance; no live providers."""
from backend.tests.run_6b1_acceptance import main


if __name__ == "__main__":
    raise SystemExit(main(stage="6bc1", base_targets=(
        "backend/tests/investigation_loop/test_cards.py",
        "backend/tests/investigation_loop/test_diagnostics.py",
        "backend/tests/investigation_loop/test_compact_budget.py",
    ), required_reports=("evidence-cards.json",)))
