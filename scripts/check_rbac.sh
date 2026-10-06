#!/usr/bin/env bash
set -Eeuo pipefail
MODE="${1:-reader}"
case "$MODE" in reader) WRITE=no ;; remediator) WRITE=yes ;; *) echo 'Usage: check_rbac.sh reader|remediator' >&2; exit 2 ;; esac
EXPECTED_CONTEXT="${RBAC_CONTEXT:-kind-incident-agent}"
SUBJECT="${RBAC_SUBJECT-system:serviceaccount:agent-demo:incident-agent}"
TARGET="${RBAC_TARGET:-order-service}"
OTHER="${RBAC_OTHER_TARGET:-not-registered}"
KUBE=(kubectl --context "$EXPECTED_CONTEXT" --request-timeout=15s)
if [[ -n "${RBAC_KUBECONFIG:-}" ]]; then KUBE+=(--kubeconfig "$RBAC_KUBECONFIG"); fi
if [[ -n "$SUBJECT" ]]; then
  KUBE+=(--as "$SUBJECT")
  echo 'Auxiliary impersonation check only; this does not prove the deployed credential.'
else
  echo 'Checking the identity in the supplied kubeconfig.'
fi
check_permission() {
  local expected="$1" actual
  shift
  actual="$("${KUBE[@]}" auth can-i "$@" 2>/dev/null || true)"
  if [[ "$actual" != "$expected" ]]; then
    echo "FAILED: expected=$expected actual=$actual permission=$*" >&2
    exit 1
  fi
  echo "PASSED: $expected <- $*"
}
check_permission yes get pods -n agent-demo
check_permission yes list pods -n agent-demo
check_permission yes get pods --subresource=log -n agent-demo
check_permission yes get services -n agent-demo
check_permission yes list events -n agent-demo
check_permission yes get deployments.apps -n agent-demo
check_permission yes get replicasets.apps -n agent-demo
check_permission yes list endpointslices.discovery.k8s.io -n agent-demo
check_permission yes get nodes
check_permission yes get services/incident-agent-business-probe:80 --subresource=proxy -n agent-demo
check_permission "$WRITE" patch "services/$TARGET" -n agent-demo
check_permission "$WRITE" patch "deployments.apps/$TARGET" -n agent-demo
check_permission no patch "services/$OTHER" -n agent-demo
check_permission no patch "deployments.apps/$OTHER" -n agent-demo
check_permission no get services/order-service --subresource=proxy -n agent-demo
check_permission no get secrets -n agent-demo
check_permission no list secrets -n agent-demo
check_permission no create pods --subresource=exec -n agent-demo
check_permission no create pods -n agent-demo
check_permission no delete pods -n agent-demo
check_permission no delete deployments.apps -n agent-demo
check_permission no create rolebindings.rbac.authorization.k8s.io -n agent-demo
check_permission no list nodes
check_permission no get pods -n kube-system
echo "PASS: $MODE authorization matrix (actual API requests are checked by accept_stage3b.sh)."
