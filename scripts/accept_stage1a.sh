#!/usr/bin/env bash
# Run from the repository root, with the existing ECS Python 3.12 venv active.
set -euo pipefail

python -c 'import sys,pytest; assert sys.version_info[:2] == (3,12), "Activate the project Python 3.12 venv"; import backend.app.main'
node --version
npm --version

PG_CONTAINER="${PG_CONTAINER:-k8s-incident-agent-postgres-1}"
TEST_DB="incident_agent_test_1a_$(date -u +%Y%m%d_%H%M%S)_$$"
AUDIT_DIR="evals/results/1a/$TEST_DB"
mkdir -p "$AUDIT_DIR"
git rev-parse HEAD | tee "$AUDIT_DIR/commit.txt"
printf '%s\n' "$TEST_DB" | tee "$AUDIT_DIR/database.txt"

# New database only. No drop/truncate, no writes to the demonstration database.
docker exec "$PG_CONTAINER" sh -c 'exec createdb -U "$POSTGRES_USER" "$1"' sh "$TEST_DB"
export INCIDENT_AGENT_TEST_DATABASE_NAME="$TEST_DB"
python -m backend.tests.run_1a_acceptance 2>&1 | tee "$AUDIT_DIR/backend.txt"
docker exec "$PG_CONTAINER" sh -c 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -c "SELECT version,name FROM incident_agent_app.schema_migrations ORDER BY version"' sh "$TEST_DB" | tee "$AUDIT_DIR/migrations.txt"

(cd frontend && npm ci && npm test && npm run build) 2>&1 | tee "$AUDIT_DIR/frontend.txt"
printf '\nPASS: 1A ECS acceptance. Evidence: %s\nTest database retained: %s\n' "$AUDIT_DIR" "$TEST_DB"
