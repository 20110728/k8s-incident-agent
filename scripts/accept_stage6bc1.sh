#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
python -c 'import sys,pytest; assert sys.version_info[:2] == (3,12), "Activate Python 3.12 venv"'
PG_CONTAINER="${PG_CONTAINER:-k8s-incident-agent-postgres-1}"
TEST_DB="incident_agent_test_6bc1_$(date -u +%Y%m%d_%H%M%S)_$$"
AUDIT_DIR="$(pwd)/evals/results/6bc1/$TEST_DB"
mkdir -p "$AUDIT_DIR"
git rev-parse HEAD | tee "$AUDIT_DIR/commit.txt"
docker exec "$PG_CONTAINER" sh -c 'exec createdb -U "$POSTGRES_USER" "$1"' sh "$TEST_DB"
export INCIDENT_AGENT_TEST_DATABASE_NAME="$TEST_DB"
export INCIDENT_AGENT_TEST_AUDIT_DIR="$AUDIT_DIR"
export INCIDENT_AGENT_TEST_CLEAN_PASSED_DATABASES="${INCIDENT_AGENT_TEST_CLEAN_PASSED_DATABASES:-1}"
python -m backend.tests.run_6bc1_acceptance 2>&1 | tee "$AUDIT_DIR/backend.txt"
printf '\nPASS: 6B-C1 ECS acceptance. Evidence: %s\n' "$AUDIT_DIR"
printf 'Program extraction and real isolated PostgreSQL; no live model/cluster calls, no frontend build.\n'
