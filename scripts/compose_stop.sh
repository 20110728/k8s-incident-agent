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

export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"
export KUBECONFIG_PATH="${KUBECONFIG_PATH:-${KUBECONFIG:-$HOME/.kube/config}}"
export KUBECONFIG_PATH="${KUBECONFIG_PATH%%:*}"
export KUBERNETES_CONTEXT="${KUBERNETES_CONTEXT:-kind-incident-agent}"
export POSTGRES_VOLUME_NAME="${POSTGRES_VOLUME_NAME:-postgres_incident-agent-postgres}"

compose=(
  docker compose
  --file "$repo_root/compose.yaml"
)

echo "Stopping frontend, backend and PostgreSQL."
echo "Containers and database volumes will not be deleted."

"${compose[@]}" stop \
  frontend \
  backend \
  postgres

"${compose[@]}" ps --all

echo
echo "Services stopped."
echo "PostgreSQL volume preserved: ${POSTGRES_VOLUME_NAME}"