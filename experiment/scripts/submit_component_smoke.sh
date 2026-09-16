#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT/scripts/common.sh"
load_site_env
key=${1:?Usage: scripts/submit_component_smoke.sh e1|e2|e3|e4|e5|e6|e7}
dir=$(component_dir "$key")
export RUN_BASE=${RUN_BASE:-$ROOT/runs/$(basename "$dir")}
(cd "$dir" && bash scripts/submit_smoke.sh)
