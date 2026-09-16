#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
run_build() {
  local dir="$1"
  shift
  (cd "$dir" && "$dir/scripts/run_python.sh" "$@")
}
run_build "$ROOT/experiments/e1_msi_storage" scripts/build_zipapp.py
run_build "$ROOT/experiments/e2_local_query" scripts/build_zipapp.py
run_build "$ROOT/experiments/e3_communication_network" scripts/build_zipapp.py --source src --output exp03_network.pyz
run_build "$ROOT/experiments/e4_concurrent_scaling" scripts/build_zipapp.py
run_build "$ROOT/experiments/e5_anchor_pruning" scripts/build_zipapp.py
run_build "$ROOT/experiments/e6_refolding_policy" scripts/build_zipapp.py --source src --output exp06_refolding_policy.pyz
run_build "$ROOT/experiments/e7_integrity_availability" scripts/build_zipapp.py
