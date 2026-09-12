#!/usr/bin/env bash
set -Eeuo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
cd "$repo_root"
kube=(kubectl --context kind-incident-agent -n agent-demo)
probe_config_dir="$(mktemp -d /tmp/incident-business-probe.XXXXXX)"
trap 'rm -rf -- "$probe_config_dir"' EXIT
python -m scripts.render_business_probe_config --output "$probe_config_dir/targets.json"
docker build -f infra/business-probe/Dockerfile -t k8s-business-probe:0.2.0 .
kind load docker-image k8s-business-probe:0.2.0 --name incident-agent
"${kube[@]}" create configmap incident-agent-business-probe-targets \
  --from-file="targets.json=$probe_config_dir/targets.json" --dry-run=client -o yaml \
  | "${kube[@]}" apply -f -
"${kube[@]}" apply -f infra/business-probe/baseline.yaml
# Avoid waiting for eventual ConfigMap volume refresh; new pods mount the current config.
"${kube[@]}" rollout restart deployment/incident-agent-business-probe
"${kube[@]}" rollout status deployment/incident-agent-business-probe --timeout=120s
