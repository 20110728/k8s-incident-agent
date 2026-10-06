#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
kubectl --context kind-incident-agent --request-timeout=15s get namespace agent-demo >/dev/null
export STAGE3B_RBAC=1
exec bash scripts/accept_stage1b.sh 3b
