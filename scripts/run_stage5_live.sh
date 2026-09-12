#!/usr/bin/env bash
set -Eeuo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir/.."
# Explicit operator-run batch. Does not call diagnosis/planning models or execute Agent plans.
failed=0
for case_id in normal selector_mismatch readiness_path_error dependency_unavailable api500 wrong_content evidence_missing; do
  if bash scripts/stage5_fault.sh "$case_id" && python -m scripts.run_fault_case --case "$case_id" --source live --mode facts; then
    printf 'PASS %s (live facts only)\n' "$case_id"
  else
    printf 'FAIL %s; inspect saved output or injection error\n' "$case_id" >&2
    failed=1
    break
  fi
done
# Try restoration even when a case failed. Never hide a restoration failure.
if ! bash scripts/stage5_fault.sh reset; then
  echo 'RESET FAILED: inspect cluster and operator guard error before continuing.' >&2
  exit 3
fi
if ! python -m scripts.run_fault_case --case normal --source live --mode facts; then
  echo 'RESET verification failed.' >&2
  exit 3
fi
exit "$failed"
