#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT/scripts/common.sh"
load_site_env
for dir in "$ROOT"/experiments/*; do
  echo "PREFLIGHT_COMPONENT=$(basename "$dir")"
  (cd "$dir" && bash scripts/preflight.sh)
done
