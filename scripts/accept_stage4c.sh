#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
AUDIT_DIR="$(pwd)/evals/results/4c/$(date -u +%Y%m%d_%H%M%S)_$$"
mkdir -p "$AUDIT_DIR"
git rev-parse HEAD | tee "$AUDIT_DIR/commit.txt"
(
  cd frontend
  node --version
  npm --version
  npm ci --include=dev
  npm test
  npm run build
) 2>&1 | tee "$AUDIT_DIR/frontend.txt"
python scripts/check_stage4c_api.py 2>&1 | tee "$AUDIT_DIR/api.txt"
printf '\nPASS: 4C automated ECS acceptance. Evidence: %s\n' "$AUDIT_DIR"
printf 'Browser acceptance is still required after rebuilding frontend; see docs/acceptance/4C-workbench.md.\n'
