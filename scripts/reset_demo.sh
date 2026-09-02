#!/usr/bin/env bash

set -Eeuo pipefail

EXPECTED_CONTEXT="kind-incident-agent"
CURRENT_CONTEXT="$(kubectl config current-context)"

SCRIPT_DIR="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
  pwd
)"

PROJECT_ROOT="$(
  cd -- "${SCRIPT_DIR}/.."
  pwd
)"

BASELINE_FILE="${PROJECT_ROOT}/infra/demo-app/baseline.yaml"

RBAC_DIR="${PROJECT_ROOT}/infra/rbac"

if [[ "${CURRENT_CONTEXT}" != "${EXPECTED_CONTEXT}" ]]; then
  echo "ERROR: current context is ${CURRENT_CONTEXT}"
  echo "Expected context: ${EXPECTED_CONTEXT}"
  exit 1
fi

if [[ ! -f "${BASELINE_FILE}" ]]; then
  echo "ERROR: baseline file not found: ${BASELINE_FILE}"
  exit 1
fi

echo "Deleting agent-demo namespace..."

kubectl delete namespace agent-demo \
  --ignore-not-found=true \
  --wait=true \
  --timeout=120s

echo "Applying healthy baseline..."

kubectl apply -f "${BASELINE_FILE}"

echo "Applying read-only RBAC..."

kubectl apply -f "${RBAC_DIR}"

echo "Waiting for order-service rollout..."

kubectl rollout status \
  deployment/order-service \
  -n agent-demo \
  --timeout=120s

echo "Checking pods..."

kubectl get pods \
  -n agent-demo \
  -l app=order-service \
  -o wide

echo "Demo environment reset successfully."