#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export STAGE3A_KIND=1
export STAGE2B_KIND=1
export STAGE2C_RECOVERY=0
exec bash scripts/accept_stage1b.sh 3a
