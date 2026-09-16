#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUT=${1:-$ROOT/local_smoke_run}
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"
"$ROOT/scripts/run_python.sh" "$ROOT/exp07_security_availability.pyz" run-local \
  --config "$ROOT/configs/smoke.json" --output "$OUT" --clean
"$ROOT/scripts/run_python.sh" "$ROOT/exp07_security_availability.pyz" validate --results "$OUT/results"
