#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"
export KUBECONFIG_PATH="$(docker inspect --format \
  '{{range .Mounts}}{{if eq .Destination "/tmp/kubeconfig"}}{{.Source}}{{end}}{{end}}' \
  k8s-incident-agent-backend-1)"
if [[ -z "$KUBECONFIG_PATH" || ! -f "$KUBECONFIG_PATH" ]]; then
  echo 'Existing backend kubeconfig mount was not found. No container was changed.' >&2
  exit 1
fi
# Compose interpolates all services even when only frontend is selected.
docker compose up -d --build --no-deps frontend
curl --fail --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 \
  http://127.0.0.1:8080/frontend-healthz
