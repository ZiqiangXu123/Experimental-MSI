#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT/scripts/common.sh"
load_site_env
key=${1:?Usage: scripts/submit_component_publication.sh e1|e2|e3|e4|e5|e6|e7 [portable|dual]}
mode=${2:-portable}
dir=$(component_dir "$key")
export RUN_BASE=${RUN_BASE:-$ROOT/runs/$(basename "$dir")}
script=scripts/submit_publication.sh
if [[ "$key" =~ ^(3|e3|E3)$ && "$mode" == dual ]]; then
  script=scripts/submit_publication_dual_node.sh
fi
if [[ "$key" =~ ^(4|e4|E4)$ && "$mode" == dual ]]; then
  script=scripts/submit_publication_dual_node.sh
fi
(cd "$dir" && bash "$script")
