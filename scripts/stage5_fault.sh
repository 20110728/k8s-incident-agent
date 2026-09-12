#!/usr/bin/env bash
set -Eeuo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir/.."
kube=(kubectl --context kind-incident-agent -n agent-demo)
action="${1:-help}"
case "$action" in
 normal|selector_mismatch|readiness_path_error|dependency_unavailable|api500|wrong_content|evidence_missing|reset) ;;
 *) echo 'Usage: bash scripts/stage5_fault.sh {normal|selector_mismatch|readiness_path_error|dependency_unavailable|api500|wrong_content|evidence_missing|reset}' >&2; exit 2;;
esac
# Reset only the known stage5 configuration faults; reject unrelated modifications.
python -m scripts.fault_cases.operator reset_configuration
python -m scripts.fault_cases.operator probe_up
"${kube[@]}" rollout status deployment/incident-agent-business-probe --timeout=120s
bash scripts/demo_v02.sh normal
case "$action" in
 selector_mismatch)
   python -m scripts.fault_cases.operator selector_mismatch
   ;;
 readiness_path_error)
   python -m scripts.fault_cases.operator readiness_path_error
   python -m scripts.fault_cases.operator wait_unready
   ;;
 dependency_unavailable|api500|wrong_content)
   bash scripts/demo_v02.sh "$action"
   ;;
 evidence_missing)
   pods="$("${kube[@]}" get pod -l app=incident-agent-business-probe -o name)"
   python -m scripts.fault_cases.operator probe_down
   if [[ -n "$pods" ]]; then
     while IFS= read -r pod; do "${kube[@]}" wait --for=delete "$pod" --timeout=90s; done <<< "$pods"
   fi
   ;;
 normal|reset) ;;
esac
printf 'Scenario prepared: %s. Capture with scripts.run_fault_case; reset with: bash scripts/stage5_fault.sh reset\n' "$action"
