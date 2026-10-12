#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Reuse the existing mount discovery and readiness checks.
bash scripts/deploy_stage6b2b_backend.sh
bash scripts/deploy_stage4c_frontend.sh
