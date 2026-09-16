#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT/scripts/common.sh"
load_site_env
key=${1:?Usage: scripts/run_component_local_smoke.sh e1|e2|e3|e4|e5|e6|e7 [run_dir]}
dir=$(component_dir "$key")
run_dir=${2:-$ROOT/runs/$(basename "$dir")/local-smoke-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$(dirname "$run_dir")"
(cd "$dir" && bash scripts/run_local_smoke.sh "$run_dir")
