#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Five bounded live model calls; cluster observations use deterministic fixtures.
export STAGE4B1_LIVE_MODEL=1
exec bash scripts/accept_stage1b.sh 4b1
