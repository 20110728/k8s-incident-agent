#!/usr/bin/env bash
set -euo pipefail
STAGE="${1:-1b}"
case "$STAGE" in 1b|2a|2b|2c|3a|3b|4a1|4a2) ;; *) echo 'Unsupported acceptance stage' >&2; exit 2 ;; esac
if [[ "$STAGE" == 3b && "${STAGE3B_RBAC:-0}" != 1 ]]; then
  echo '3B requires real credential checks; run bash scripts/accept_stage3b.sh' >&2
  exit 2
fi
if [[ "$STAGE" == 3a && "${STAGE3A_KIND:-0}" != 1 ]]; then
  echo '3A requires real kind checks; run bash scripts/accept_stage3a.sh' >&2
  exit 2
fi
if [[ "$STAGE" == 2c && ( "${STAGE2C_RECOVERY:-0}" != 1 || "${STAGE2B_KIND:-0}" != 1 ) ]]; then
  echo '2C requires process recovery and real kind checks; run bash scripts/accept_stage2c.sh' >&2
  exit 2
fi
python -c 'import sys,pytest; assert sys.version_info[:2] == (3,12), "Activate Python 3.12 venv"; import backend.app.runtime.worker'
if [[ "$STAGE" == 1b || "$STAGE" == 2b ]]; then node --version; npm --version; fi
PG_CONTAINER="${PG_CONTAINER:-k8s-incident-agent-postgres-1}"
TEST_DB="incident_agent_test_${STAGE}_$(date -u +%Y%m%d_%H%M%S)_$$"
AUDIT_DIR="evals/results/$STAGE/$TEST_DB"
mkdir -p "$AUDIT_DIR"
git rev-parse HEAD | tee "$AUDIT_DIR/commit.txt"
printf '%s\n' "$TEST_DB" | tee "$AUDIT_DIR/database.txt"
docker exec "$PG_CONTAINER" sh -c 'exec createdb -U "$POSTGRES_USER" "$1"' sh "$TEST_DB"
export INCIDENT_AGENT_TEST_DATABASE_NAME="$TEST_DB"
export INCIDENT_AGENT_TEST_AUDIT_DIR="$AUDIT_DIR"
python -m backend.tests.run_1b_acceptance 2>&1 | tee "$AUDIT_DIR/backend.txt"
docker exec "$PG_CONTAINER" sh -c 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -c "SELECT version,name FROM incident_agent_app.schema_migrations ORDER BY version"' sh "$TEST_DB" | tee "$AUDIT_DIR/migrations.txt"
if [[ "$STAGE" == 1b || "$STAGE" == 2b ]]; then
  (cd frontend && npm ci && npm test && npm run build) 2>&1 | tee "$AUDIT_DIR/frontend.txt"
fi
if [[ "$STAGE" == 2b && "${STAGE2B_KIND:-0}" != 1 ]]; then
  printf '\nPASS: 2B simulated acceptance only (kind not run). Evidence: %s\n' "$AUDIT_DIR"
else
  printf '\nPASS: %s ECS acceptance. Evidence: %s\nTest databases retained.\n' "${STAGE^^}" "$AUDIT_DIR"
fi
