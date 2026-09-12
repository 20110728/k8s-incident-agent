#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
cd "$repo_root"

# This is an operator-run Demo script, never an Agent tool or approval bypass.
kube=(kubectl --context kind-incident-agent -n agent-demo)
command="${1:-help}"
case "$command" in
  deploy)
    docker build -t k8s-incident-demo:0.2.0 infra/demo-app
    kind load docker-image k8s-incident-demo:0.2.0 --name incident-agent
    "${kube[@]}" apply -f infra/demo-app/baseline.yaml
    "${kube[@]}" rollout status deployment/order-dependency --timeout=120s
    "${kube[@]}" rollout status deployment/order-service --timeout=120s
    ;;
  normal|api500|wrong_content|dependency_unavailable)
    release="$("${kube[@]}" get deployment order-service -o 'jsonpath={.spec.template.metadata.labels.app\.kubernetes\.io/version}')"
    if [[ "$release" != "order-demo-v0.2.0" ]]; then
      echo "Deploy the stage-2 Demo first; refusing to modify a different release." >&2
      exit 1
    fi
    # Reset the previous scenario before switching, so failures do not accumulate.
    "${kube[@]}" scale deployment/order-dependency --replicas=1
    "${kube[@]}" rollout status deployment/order-dependency --timeout=120s
    mode="$command"
    if [[ "$command" == "dependency_unavailable" ]]; then mode=normal; fi
    "${kube[@]}" set env deployment/order-service "ORDER_FAULT_MODE=$mode"
    "${kube[@]}" rollout status deployment/order-service --timeout=120s
    if [[ "$command" == "dependency_unavailable" ]]; then
      dependency_pods="$("${kube[@]}" get pod -l app=order-dependency -o name)"
      "${kube[@]}" scale deployment/order-dependency --replicas=0
      if [[ -n "$dependency_pods" ]]; then
        while IFS= read -r dependency_pod; do
          "${kube[@]}" wait --for=delete "$dependency_pod" --timeout=90s
        done <<< "$dependency_pods"
      fi
      # Wait for the readiness consequence without changing or relaxing probes.
      "${kube[@]}" wait --for=condition=Ready=false pod -l app=order-service --timeout=60s
    fi
    ;;
  inspect)
    scenario="${2:-}"
    case "$scenario" in normal|api500|wrong_content|dependency_unavailable) ;; *)
      echo "Usage: bash scripts/demo_v02.sh inspect <scenario>" >&2; exit 2;;
    esac
    "${kube[@]}" exec deployment/order-service -c order-service -- \
      python -m demo_app.inspect --expect "$scenario"
    ;;
  *)
    echo "Usage: bash scripts/demo_v02.sh {deploy|normal|api500|wrong_content|dependency_unavailable|inspect <scenario>}"
    exit 2
    ;;
esac
