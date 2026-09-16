#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT/scripts/common.sh"
load_site_env
for key in e1 e2 e3 e4 e5 e6 e7; do
  dir=$(component_dir "$key")
  echo "LOCAL_SMOKE_COMPONENT=$(basename "$dir")"
  bash "$ROOT/scripts/run_component_local_smoke.sh" "$key"
done
