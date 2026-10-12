#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
python -c 'import sys,pytest; assert sys.version_info[:2] == (3,12), "Activate Python 3.12 venv"'
PG_CONTAINER="${PG_CONTAINER:-k8s-incident-agent-postgres-1}"
TEST_DB="incident_agent_test_6b2b_$(date -u +%Y%m%d_%H%M%S)_$$"
AUDIT_DIR="$(pwd)/evals/results/6b2b/$TEST_DB"
mkdir -p "$AUDIT_DIR"
git rev-parse HEAD | tee "$AUDIT_DIR/commit.txt"
docker exec "$PG_CONTAINER" sh -c 'exec createdb -U "$POSTGRES_USER" "$1"' sh "$TEST_DB"
export INCIDENT_AGENT_TEST_DATABASE_NAME="$TEST_DB"
export INCIDENT_AGENT_TEST_AUDIT_DIR="$AUDIT_DIR"
python -m backend.tests.run_6b2b_acceptance 2>&1 | tee "$AUDIT_DIR/backend.txt"
printf '\nPASS: 6B-2b ECS acceptance. Evidence: %s\n' "$AUDIT_DIR"
printf 'Real PostgreSQL/checkpoints/API/worker; controlled model/Kubernetes. Test databases retained.\n'
