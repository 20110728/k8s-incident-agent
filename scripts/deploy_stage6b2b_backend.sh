#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"
if [ -z "${KUBECONFIG_PATH:-}" ]; then
  export KUBECONFIG_PATH="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/tmp/kubeconfig"}}{{.Source}}{{end}}{{end}}' k8s-incident-agent-backend-1)"
fi
if [ -z "$KUBECONFIG_PATH" ] || [ ! -f "$KUBECONFIG_PATH" ]; then
  echo 'Existing kubeconfig mount was not found; set KUBECONFIG_PATH to its host file.' >&2
  exit 1
fi
docker compose --profile worker up -d --no-deps --force-recreate backend worker
curl --fail --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 http://127.0.0.1:8000/readyz
printf '\n'
for service in backend worker; do
  printf '%s settings: ' "$service"
  docker compose --profile worker exec -T "$service" python -c \
    'from backend.app.config import get_api_settings; s=get_api_settings(); print({"execution_mode":s.execution_mode,"investigation_enabled":s.investigation_enabled}); assert s.execution_mode == "queued", "queued mode required"'
done
docker compose --profile worker ps backend worker
