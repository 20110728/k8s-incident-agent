#!/usr/bin/env bash

set -Eeuo pipefail

script_dir="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
  pwd
)"
repo_root="$(
  cd -- "${script_dir}/.."
  pwd
)"

cd "$repo_root"

for required_command in docker kubectl curl; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    echo "Required command is unavailable: ${required_command}" >&2
    exit 1
  fi
done

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is unavailable." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "Missing ${repo_root}/.env" >&2
  echo "Copy .env.example to .env and configure it first." >&2
  exit 1
fi

kubeconfig_path="${KUBECONFIG_PATH:-${KUBECONFIG:-$HOME/.kube/config}}"

# Kubernetes supports a colon-separated KUBECONFIG list.
# The current local setup uses one file, so mount the first entry.
kubeconfig_path="${kubeconfig_path%%:*}"

if [[ ! -f "$kubeconfig_path" ]]; then
  echo "Kubeconfig file does not exist: ${kubeconfig_path}" >&2
  exit 1
fi

if [[ ! -r "$kubeconfig_path" ]]; then
  echo "Kubeconfig file is not readable: ${kubeconfig_path}" >&2
  exit 1
fi

kubernetes_context="${KUBERNETES_CONTEXT:-$(kubectl --kubeconfig "$kubeconfig_path" config current-context)}"

if ! kubectl \
  --kubeconfig "$kubeconfig_path" \
  config get-contexts "$kubernetes_context" \
  -o name \
  | grep -Fxq "$kubernetes_context"; then
  echo "Kubernetes context was not found: ${kubernetes_context}" >&2
  exit 1
fi

export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"
export KUBECONFIG_PATH="$kubeconfig_path"
export KUBERNETES_CONTEXT="$kubernetes_context"
export POSTGRES_VOLUME_NAME="${POSTGRES_VOLUME_NAME:-postgres_incident-agent-postgres}"

compose=(
  docker compose
  --file "$repo_root/compose.yaml"
)

show_failure_context() {
  exit_code=$?
  trap - ERR

  echo "Compose startup failed with exit code ${exit_code}." >&2
  "${compose[@]}" ps --all || true
  "${compose[@]}" logs --tail 80 postgres backend frontend || true

  exit "$exit_code"
}

trap show_failure_context ERR

echo "Using Kubernetes context: ${KUBERNETES_CONTEXT}"
echo "Using kubeconfig: ${KUBECONFIG_PATH}"
echo "Using PostgreSQL volume: ${POSTGRES_VOLUME_NAME}"
echo "Using runtime UID:GID: ${LOCAL_UID}:${LOCAL_GID}"

if ! docker volume inspect \
  "$POSTGRES_VOLUME_NAME" \
  >/dev/null 2>&1; then
  echo "Creating PostgreSQL volume: ${POSTGRES_VOLUME_NAME}"

  docker volume create \
    --label com.k8s-incident-agent.managed=true \
    "$POSTGRES_VOLUME_NAME" \
    >/dev/null
else
  echo "Reusing PostgreSQL volume: ${POSTGRES_VOLUME_NAME}"
fi

legacy_postgres="postgres-postgres-1"

if docker inspect \
  "$legacy_postgres" \
  >/dev/null 2>&1; then
  legacy_running="$(
    docker inspect \
      --format '{{.State.Running}}' \
      "$legacy_postgres"
  )"

  if [[ "$legacy_running" == "true" ]]; then
    echo "Stopping legacy PostgreSQL container without deleting it."
    docker stop "$legacy_postgres" >/dev/null
  fi
fi

echo "Validating Compose configuration."
"${compose[@]}" config --quiet

echo "Building application images."
"${compose[@]}" build backend frontend

echo "Starting PostgreSQL."
"${compose[@]}" up --detach postgres

postgres_ready="false"

for attempt in $(seq 1 30); do
  if "${compose[@]}" exec -T postgres \
    sh -lc \
    'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    >/dev/null 2>&1; then
    postgres_ready="true"
    break
  fi

  sleep 1
done

if [[ "$postgres_ready" != "true" ]]; then
  echo "PostgreSQL did not become ready within 30 seconds." >&2
  exit 1
fi

echo "PostgreSQL is ready."

embedding_count="0"

if "${compose[@]}" exec -T postgres \
  sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "\dt public.langchain_pg_embedding"' \
  | grep -q 'langchain_pg_embedding'; then
  embedding_count="$(
    "${compose[@]}" exec -T postgres \
      sh -lc \
      'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM public.langchain_pg_embedding;"'
  )"

  embedding_count="$(
    printf '%s' "$embedding_count" \
      | tr -d '[:space:]'
  )"
fi

if [[ "$embedding_count" =~ ^[0-9]+$ ]] \
  && (( embedding_count > 0 )); then
  echo "Existing Runbook vectors detected: ${embedding_count}"
  echo "Skipping Runbook indexing."
else
  echo "No Runbook vectors detected."
  echo "Running the one-time Runbook indexer."

  "${compose[@]}" \
    --profile bootstrap \
    run \
    --rm \
    runbook-indexer
fi

echo "Starting backend and frontend."
"${compose[@]}" up \
  --detach \
  backend \
  frontend

wait_for_url() {
  local label="$1"
  local url="$2"
  local attempts="${3:-60}"

  for attempt in $(seq 1 "$attempts"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "${label} is ready: ${url}"
      return 0
    fi

    sleep 1
  done

  echo "${label} did not become ready: ${url}" >&2
  return 1
}

wait_for_url \
  "Backend" \
  "http://127.0.0.1:8000/readyz" \
  60

wait_for_url \
  "Frontend" \
  "http://127.0.0.1:8080/frontend-healthz" \
  30

"${compose[@]}" ps

echo
echo "Kubernetes Incident Agent is running."
echo "Frontend: http://127.0.0.1:8080"
echo "Backend health: http://127.0.0.1:8000/healthz"
echo "Backend readiness: http://127.0.0.1:8000/readyz"