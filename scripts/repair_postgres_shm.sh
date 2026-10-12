#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
PG_CONTAINER=k8s-incident-agent-postgres-1
volume="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{if eq .Type "volume"}}{{.Name}}{{end}}{{end}}{{end}}' "$PG_CONTAINER")"
if [ -z "$volume" ]; then
  echo 'Expected an existing named PostgreSQL data volume; nothing changed.' >&2
  exit 1
fi
export POSTGRES_VOLUME_NAME="$volume"
export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"
export KUBECONFIG_PATH="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/tmp/kubeconfig"}}{{.Source}}{{end}}{{end}}' k8s-incident-agent-backend-1)"
if [ -z "$KUBECONFIG_PATH" ] || [ ! -f "$KUBECONFIG_PATH" ]; then
  echo 'Existing backend kubeconfig mount not found; nothing changed.' >&2
  exit 1
fi
# Validate the service without printing the resolved configuration/credentials.
docker compose -f compose.yaml config --quiet
printf 'Retaining PostgreSQL data volume: %s\n' "$volume"
resume=()
for name in k8s-incident-agent-worker-1 k8s-incident-agent-backend-1; do
  if [ "$(docker inspect --format '{{.State.Running}}' "$name" 2>/dev/null || true)" = true ]; then
    docker stop --time 45 "$name"
    resume+=("$name")
  fi
done
trap 'echo "Recovery did not finish. Application containers may remain stopped; report the error before retrying." >&2' ERR
# Explicitly reuse the detected external volume; never down -v or remove data.
docker compose -f compose.yaml up -d --no-deps --force-recreate postgres
after="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' "$PG_CONTAINER")"
test "$after" = "$volume"
test "$(docker inspect --format '{{.HostConfig.ShmSize}}' "$PG_CONTAINER")" = 268435456
ready=false
for attempt in {1..20}; do
  if docker exec "$PG_CONTAINER" sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -Atqc "SELECT 1"' >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 2
done
test "$ready" = true
docker exec "$PG_CONTAINER" df -h /dev/shm
if [ "${#resume[@]}" -gt 0 ]; then
  docker start "${resume[@]}"
fi
curl --fail --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 http://127.0.0.1:8000/readyz
printf '\nPASS: PostgreSQL shm=256MiB; original data volume retained.\n'
