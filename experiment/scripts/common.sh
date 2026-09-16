#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
component_dir() {
  case "${1:-}" in
    1|e1|E1) printf '%s\n' "$ROOT/experiments/e1_msi_storage" ;;
    2|e2|E2) printf '%s\n' "$ROOT/experiments/e2_local_query" ;;
    3|e3|E3) printf '%s\n' "$ROOT/experiments/e3_communication_network" ;;
    4|e4|E4) printf '%s\n' "$ROOT/experiments/e4_concurrent_scaling" ;;
    5|e5|E5) printf '%s\n' "$ROOT/experiments/e5_anchor_pruning" ;;
    6|e6|E6) printf '%s\n' "$ROOT/experiments/e6_refolding_policy" ;;
    7|e7|E7) printf '%s\n' "$ROOT/experiments/e7_integrity_availability" ;;
    *) echo "Usage: $0 e1|e2|e3|e4|e5|e6|e7" >&2; exit 64 ;;
  esac
}
component_name() {
  basename "$(component_dir "$1")"
}
load_site_env() {
  if [[ -f "$ROOT/scripts/site_env.sh" ]]; then
    source "$ROOT/scripts/site_env.sh"
  fi
}
