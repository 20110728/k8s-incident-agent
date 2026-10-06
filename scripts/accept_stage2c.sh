#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export STAGE2C_RECOVERY=1
export STAGE2B_KIND=1
exec bash scripts/accept_stage1b.sh 2c
