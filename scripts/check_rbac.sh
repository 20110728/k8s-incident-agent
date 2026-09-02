#!/usr/bin/env bash

set -Eeuo pipefail

EXPECTED_CONTEXT="kind-incident-agent"
CURRENT_CONTEXT="$(kubectl config current-context)"
SUBJECT="system:serviceaccount:agent-demo:incident-agent"

if [[ "${CURRENT_CONTEXT}" != "${EXPECTED_CONTEXT}" ]]; then
  echo "ERROR: current context is ${CURRENT_CONTEXT}"
  exit 1
fi

check_permission() {
  local expected="$1"
  shift

  local actual

  actual="$(
    kubectl auth can-i \
      "$@" \
      --as="${SUBJECT}" \
      2>/dev/null || true
  )"

  if [[ "${actual}" != "${expected}" ]]; then
    echo "FAILED: expected=${expected}, actual=${actual}"
    echo "COMMAND: kubectl auth can-i $*"
    exit 1
  fi

  echo "PASSED: ${expected} <- kubectl auth can-i $*"
}

echo "Checking allowed operations..."

check_permission yes get pods -n agent-demo
check_permission yes list pods -n agent-demo
check_permission yes get pods/log -n agent-demo
check_permission yes get services -n agent-demo
check_permission yes list events -n agent-demo
check_permission yes get deployments.apps -n agent-demo
check_permission yes get replicasets.apps -n agent-demo
check_permission yes list endpointslices.discovery.k8s.io \
  -n agent-demo
check_permission yes get nodes

echo "Checking denied operations..."

check_permission no get secrets -n agent-demo
check_permission no list secrets -n agent-demo
check_permission no delete pods -n agent-demo
check_permission no create pods -n agent-demo
check_permission no patch deployments.apps -n agent-demo
check_permission no delete deployments.apps -n agent-demo
check_permission no create rolebindings.rbac.authorization.k8s.io \
  -n agent-demo
check_permission no get pods -n kube-system

echo "All RBAC checks passed."